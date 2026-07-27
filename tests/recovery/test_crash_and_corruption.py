"""Failure behaviour: what survives, what does not, and what is merely detected.

There is no write-ahead log yet, so the engine cannot *repair* anything.  What
it can do — and what these tests pin down — is fail loudly and specifically
instead of returning wrong answers.  Milestone 9 turns each of these
"detected" outcomes into a "recovered" one.

Milestone 8 added transactions, and the boundary they draw is the important
thing to keep honest: **a rollback in-process is atomic, a crash is not.** The
undo log lives in memory and the buffer pool may already have written
uncommitted pages out, so a process killed mid-transaction leaves whatever the
pool happened to evict. The last two tests here assert exactly that, so nobody
reads "ChenDB has transactions" as "ChenDB has crash atomicity".

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
    db.insert("t", (i, f"synced-{{i}}"))
db.sync()
for i in range({synced}, {synced} + {unsynced}):
    db.insert("t", (i, f"unsynced-{{i}}"))
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
        rows = db.rows("t")
        assert len(rows) == 20
        assert rows[0] == (0, "synced-0")


def test_the_file_is_still_openable_after_a_kill_mid_workload(tmp_path: Path):
    # Writes after the fsync may or may not have reached the disk. Either
    # outcome is acceptable; an unopenable file is not.
    path = tmp_path / "mixed.chendb"
    crash_after_writing(path, synced=10, unsynced=25)

    with Database.open(path) as db:
        rows = db.rows("t")
        ids = [row[0] for row in rows]
        assert ids[:10] == list(range(10)), "acknowledged, synced rows must survive"
        assert len(rows) >= 10
        assert db.table("t") is not None


def test_a_killed_process_leaves_a_whole_number_of_pages(tmp_path: Path):
    path = tmp_path / "aligned.chendb"
    crash_after_writing(path, synced=5, unsynced=40)
    assert path.stat().st_size % PAGE_SIZE == 0


# -- corruption ------------------------------------------------------------


def make_database(path: Path, rows: int = 5) -> None:
    with Database.open(path, page_size=PAGE_SIZE) as db:
        db.create_table("t", SIMPLE_SCHEMA)
        db.insert_many("t", [(i, f"row-{i}") for i in range(rows)])


def test_a_torn_data_page_is_detected_not_silently_returned(tmp_path: Path):
    path = tmp_path / "torn.chendb"
    make_database(path)

    raw = bytearray(path.read_bytes())
    # Overwrite the middle of the last page, as a partial sector write would.
    page_id = len(raw) // PAGE_SIZE - 1
    raw[page_id * PAGE_SIZE + 128 : page_id * PAGE_SIZE + 160] = b"\xde\xad\xbe\xef" * 8
    path.write_bytes(bytes(raw))

    with Database.open(path) as db, pytest.raises(ChecksumMismatchError):
        db.rows("t")


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
        db.insert("t", (1, "ok"))
    with Database.open(path) as db:
        assert db.rows("t") == [(1, "ok")]


def test_corruption_is_localised_to_the_damaged_page(tmp_path: Path):
    """A bad page must not make the rest of the table unreadable.

    Verified through the inspector, which reads pages independently. The scan
    path still raises on the damaged page — recovering *past* it needs the WAL.
    """
    path = tmp_path / "localised.chendb"
    make_database(path, rows=60)

    with Database.open(path) as db:
        heap_pages = sorted(db.heap_page_ids("t"))
    victim = heap_pages[len(heap_pages) // 2]

    raw = bytearray(path.read_bytes())
    raw[victim * PAGE_SIZE + 64] ^= 0xFF
    path.write_bytes(bytes(raw))

    with Database.open(path, verify_checksums=False) as db:
        summaries = {s.page_id: s for s in db.page_summaries()}
        assert summaries[victim].checksum_valid is False
        assert all(
            summaries[page_id].checksum_valid for page_id in heap_pages if page_id != victim
        )


# -- transactions across a crash (Milestone 8) -----------------------------

_TRANSACTION_TEMPLATE = """
import os, signal, sys
sys.path.insert(0, {repo!r})
from engine import Column, DataType, Database, Schema

schema = Schema.of(
    Column("id", DataType.INTEGER, nullable=False),
    Column("payload", DataType.TEXT, nullable=False),
)
db = Database.open({path!r}, page_size={page_size}, buffer_pool_frames=4)
db.create_table("t", schema)
db.insert_many("t", [(i, f"committed-{{i}}") for i in range(20)])
db.sync()

# An open transaction, deliberately never committed. With a four-frame pool and
# this many rows, the pool is forced to steal — some of these pages reach the
# disk despite belonging to a transaction that will never commit.
db.begin()
db.insert_many("t", [(1000 + i, f"uncommitted-{{i}}") for i in range(200)])
sys.stdout.write("ready")
sys.stdout.flush()
os.kill(os.getpid(), signal.SIGKILL)
"""


def crash_mid_transaction(path: Path) -> None:
    source = _TRANSACTION_TEMPLATE.format(
        repo=str(REPO_ROOT), path=str(path), page_size=PAGE_SIZE
    )
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout == "ready", completed.stderr
    assert completed.returncode == -signal.SIGKILL


def readable_rows(db: Database) -> list[tuple]:
    """Scan until a page the crash left incomplete stops us.

    A partial scan is the honest outcome: the pages that reached the disk are
    whole and readable, and the first one that did not is *detected* rather than
    returned as plausible-looking garbage.
    """
    rows: list[tuple] = []
    try:
        for _, row in db.scan("t"):
            rows.append(row)
    except ChecksumMismatchError:
        pass
    return rows


def test_a_crash_mid_transaction_leaves_a_file_that_opens(tmp_path: Path):
    """Opening must work even though the transaction never finished.

    This is the one that found a real bug. The buffer pool is free to evict the
    meta page before the pages it references, so a crash could leave a file
    *shorter* than its own page count — which the length check correctly refuses
    to open. ``allocate_page`` now extends the file at allocation time, which is
    the ordering Milestones 1-6 had for free by writing everything immediately.
    """
    path = tmp_path / "crashed-txn.chendb"
    crash_mid_transaction(path)

    with Database.open(path, page_size=PAGE_SIZE) as db:
        assert db.page_count > 1


def test_a_page_the_crash_never_flushed_is_detected_not_returned(tmp_path: Path):
    # A page allocated but never written is zeros, and zeros are not a valid
    # checksum. Detected, not repaired — the contract until Milestone 9.
    path = tmp_path / "unflushed.chendb"
    crash_mid_transaction(path)

    with Database.open(path, page_size=PAGE_SIZE) as db:
        rows = readable_rows(db)
        assert rows, "the pages that did reach the disk are readable"
        assert all(isinstance(row[0], int) for row in rows)


def test_a_crash_mid_transaction_is_not_atomic(tmp_path: Path):
    """The boundary, asserted rather than assumed.

    An uncommitted transaction that outgrew the buffer pool has had pages
    *stolen* — written to disk before it committed. The undo log died with the
    process, and nothing on disk says a transaction was open, so those rows are
    simply there.

    This is what a write-ahead log fixes, and it is the whole reason Milestone 9
    exists. Pinning uncommitted pages would not have fixed it either: a crash
    during the commit flush leaves the same partial state, and only a durable
    commit *record* can tell a complete transaction from an interrupted one.
    """
    path = tmp_path / "not-atomic.chendb"
    crash_mid_transaction(path)

    with Database.open(path, page_size=PAGE_SIZE) as db:
        uncommitted = [row for row in readable_rows(db) if row[0] >= 1000]

    assert uncommitted, (
        "expected some uncommitted rows to have survived — if this ever fails, "
        "either the pool stopped stealing or Milestone 9 landed, and this test "
        "should become an assertion that they were rolled back"
    )
