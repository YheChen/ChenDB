"""Failure behaviour in Milestone 1.

There is no write-ahead log yet, so the engine cannot *repair* anything.  What
it can do — and what these tests pin down — is fail loudly and specifically
instead of returning wrong answers.  Milestone 9 turns each of these
"detected" outcomes into a "recovered" one.

The crash simulations kill a child process with ``SIGKILL``.  Nothing runs on
the way out: no ``close()``, no ``fsync``, no atexit hook.  That is the only
honest way to test durability, because any cooperative shutdown path would
quietly flush the very buffers whose loss is under test.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from engine.database import Database
from engine.errors import ChecksumMismatchError, CorruptDatabaseError
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

PAGE_SIZE = 256
REPO_ROOT = Path(__file__).resolve().parents[2]

SIMPLE_SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False),
    Column("payload", DataType.TEXT, nullable=False),
)

_CHILD_TEMPLATE = """
import os, signal, sys
sys.path.insert(0, {repo!r})
from engine import Column, DataType, Database, Schema

schema = Schema.of(
    Column("id", DataType.INTEGER, nullable=False),
    Column("payload", DataType.TEXT, nullable=False),
)
db = Database.open({path!r}, page_size={page_size})
db.create_table("t", schema)
for i in range({synced}):
    db.insert((i, f"synced-{{i}}"))
db.sync()
for i in range({synced}, {synced} + {unsynced}):
    db.insert((i, f"unsynced-{{i}}"))
sys.stdout.write("ready")
sys.stdout.flush()
# Die abruptly. No close(), no fsync, no cleanup handlers.
os.kill(os.getpid(), signal.SIGKILL)
"""


def crash_after_writing(path: Path, *, synced: int, unsynced: int) -> None:
    """Run a child that writes, then dies without a clean shutdown."""
    source = _CHILD_TEMPLATE.format(
        repo=str(REPO_ROOT),
        path=str(path),
        page_size=PAGE_SIZE,
        synced=synced,
        unsynced=unsynced,
    )
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout == "ready", completed.stderr
    assert completed.returncode == -signal.SIGKILL, (
        f"child exited {completed.returncode} instead of being killed"
    )


# -- crash -----------------------------------------------------------------


def test_synced_rows_survive_an_abrupt_kill(tmp_path: Path):
    path = tmp_path / "killed.chendb"
    crash_after_writing(path, synced=20, unsynced=0)

    with Database.open(path) as db:
        rows = db.rows()
        assert len(rows) == 20
        assert rows[0] == (0, "synced-0")


def test_the_file_is_still_openable_after_a_kill_mid_workload(tmp_path: Path):
    # Writes after the fsync may or may not have reached the disk. Either
    # outcome is acceptable; an unopenable file is not.
    path = tmp_path / "mixed.chendb"
    crash_after_writing(path, synced=10, unsynced=25)

    with Database.open(path) as db:
        rows = db.rows()
        ids = [row[0] for row in rows]
        assert ids[:10] == list(range(10)), "acknowledged, synced rows must survive"
        assert len(rows) >= 10
        assert db.table is not None and db.table.name == "t"


def test_a_killed_process_leaves_a_whole_number_of_pages(tmp_path: Path):
    path = tmp_path / "aligned.chendb"
    crash_after_writing(path, synced=5, unsynced=40)
    assert path.stat().st_size % PAGE_SIZE == 0


# -- corruption ------------------------------------------------------------


def make_database(path: Path, rows: int = 5) -> None:
    with Database.open(path, page_size=PAGE_SIZE) as db:
        db.create_table("t", SIMPLE_SCHEMA)
        db.insert_many([(i, f"row-{i}") for i in range(rows)])


def test_a_torn_data_page_is_detected_not_silently_returned(tmp_path: Path):
    path = tmp_path / "torn.chendb"
    make_database(path)

    raw = bytearray(path.read_bytes())
    # Overwrite the middle of the last page, as a partial sector write would.
    page_id = len(raw) // PAGE_SIZE - 1
    raw[page_id * PAGE_SIZE + 128 : page_id * PAGE_SIZE + 160] = b"\xde\xad\xbe\xef" * 8
    path.write_bytes(bytes(raw))

    with Database.open(path) as db, pytest.raises(ChecksumMismatchError):
        db.rows()


def test_a_damaged_meta_page_fails_the_open_rather_than_the_first_query(
    tmp_path: Path,
):
    path = tmp_path / "bad-meta.chendb"
    make_database(path)

    raw = bytearray(path.read_bytes())
    raw[28:32] = (12345).to_bytes(4, "little")  # free_list_head
    path.write_bytes(bytes(raw))

    with pytest.raises(ChecksumMismatchError, match="meta page"):
        Database.open(path)


def test_truncation_is_detected_on_open(tmp_path: Path):
    path = tmp_path / "short.chendb"
    make_database(path, rows=60)

    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 2 * PAGE_SIZE])

    with pytest.raises(CorruptDatabaseError, match="truncated"):
        Database.open(path)


def test_a_foreign_file_is_not_mistaken_for_a_database(tmp_path: Path):
    path = tmp_path / "photo.chendb"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(4096))
    with pytest.raises(CorruptDatabaseError, match="bad magic"):
        Database.open(path)


def test_an_empty_file_is_initialised_rather_than_rejected(tmp_path: Path):
    # Some tools create a zero-byte file before writing to it.
    path = tmp_path / "empty.chendb"
    path.touch()
    with Database.open(path, page_size=PAGE_SIZE) as db:
        db.create_table("t", SIMPLE_SCHEMA)
        db.insert((1, "ok"))
    with Database.open(path) as db:
        assert db.rows() == [(1, "ok")]


def test_corruption_is_localised_to_the_damaged_page(tmp_path: Path):
    """A bad page must not make the rest of the table unreadable.

    Verified through the inspector, which reads pages independently. The scan
    path still raises on the damaged page — recovering *past* it needs the WAL.
    """
    path = tmp_path / "localised.chendb"
    make_database(path, rows=60)

    with Database.open(path) as db:
        heap_pages = sorted(db.heap_page_ids())
    victim = heap_pages[len(heap_pages) // 2]

    raw = bytearray(path.read_bytes())
    raw[victim * PAGE_SIZE + 64] ^= 0xFF
    path.write_bytes(bytes(raw))

    with Database.open(path, verify_checksums=False) as db:
        summaries = {s.page_id: s for s in db.page_summaries()}
        assert summaries[victim].checksum_valid is False
        assert all(
            summaries[page_id].checksum_valid
            for page_id in heap_pages
            if page_id != victim
        )
