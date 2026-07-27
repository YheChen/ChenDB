"""Failure behaviour: what survives, what is repaired, and what is detected.

Since Milestone 9 the engine can *repair* a crash rather than only surviving
one, and these tests draw the new line:

* **A committed transaction survives**, whether or not its pages ever reached
  the database file. The commit record did, and redo does the rest.
* **An uncommitted transaction is undone**, even though the buffer pool had
  already written some of its pages out. That is the test at the bottom of this
  file, and until this milestone it asserted the exact opposite.
* **Physical corruption is still only detected.** A page someone overwrote with
  garbage is not something a log can fix, because the log has no record of it
  happening. Those tests are unchanged and should stay that way.

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


# -- transactions across a crash -------------------------------------------

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


_COMMITTED_TEMPLATE = """
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

# Commit and die. No sync, no close: the pages are still in the pool, and the
# only thing on the disk that knows about them is the log.
db.begin()
db.insert_many("t", [(1000 + i, f"durable-{{i}}") for i in range(200)])
db.commit()
sys.stdout.write("ready")
sys.stdout.flush()
os.kill(os.getpid(), signal.SIGKILL)
"""

_CHECKPOINT_TEMPLATE = """
import os, signal, sys
sys.path.insert(0, {repo!r})
from engine import Column, DataType, Database, Schema

schema = Schema.of(
    Column("id", DataType.INTEGER, nullable=False),
    Column("payload", DataType.TEXT, nullable=False),
)
db = Database.open({path!r}, page_size={page_size}, buffer_pool_frames=4)
db.create_table("t", schema)
db.insert_many("t", [(i, f"before-{{i}}") for i in range(30)])
db.checkpoint()          # the log restarts here, at a non-zero base LSN

db.begin()
db.insert_many("t", [(1000 + i, f"after-{{i}}") for i in range(30)])
db.commit()
sys.stdout.write("ready")
sys.stdout.flush()
os.kill(os.getpid(), signal.SIGKILL)
"""


def _crash_with(template: str, path: Path) -> None:
    source = template.format(repo=str(REPO_ROOT), path=str(path), page_size=PAGE_SIZE)
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout == "ready", completed.stderr
    assert completed.returncode == -signal.SIGKILL


def crash_mid_transaction(path: Path) -> None:
    _crash_with(_TRANSACTION_TEMPLATE, path)


def crash_after_committing(path: Path) -> None:
    _crash_with(_COMMITTED_TEMPLATE, path)


def crash_after_checkpoint(path: Path) -> None:
    _crash_with(_CHECKPOINT_TEMPLATE, path)


def rows_after_recovery(path: Path) -> tuple[list[tuple], object]:
    """Open the database — which recovers it — and read every row."""
    with Database.open(path, page_size=PAGE_SIZE) as db:
        return [row for _, row in db.scan("t")], db.pager.recovery


def test_a_crash_mid_transaction_leaves_a_file_that_opens(tmp_path: Path):
    """Opening must work even though the transaction never finished.

    This is the one that found a real bug, back in Milestone 8. The buffer pool
    is free to evict the meta page before the pages it references, so a crash
    could leave a file *shorter* than its own page count — which the length
    check correctly refuses to open.
    """
    path = tmp_path / "crashed-txn.chendb"
    crash_mid_transaction(path)

    with Database.open(path, page_size=PAGE_SIZE) as db:
        assert db.page_count > 1


def test_recovery_runs_and_says_so(tmp_path: Path):
    path = tmp_path / "reports.chendb"
    crash_mid_transaction(path)

    _, report = rows_after_recovery(path)
    assert report.ran, "a crash leaves records in the log"
    assert report.losers, "the open transaction never committed"
    assert not report.winners or report.winners
    assert report.pages_undone > 0
    assert "undone" in report.summary()


def test_a_clean_shutdown_needs_no_recovery(tmp_path: Path):
    """The other half of the claim, and the one that keeps it meaningful.

    ``ran`` has to mean "the last process did not shut down cleanly". If a
    normal close left records behind, every open would recover and the flag
    would say nothing.
    """
    path = tmp_path / "clean.chendb"
    with Database.open(path, page_size=PAGE_SIZE) as db:
        db.create_table("t", SIMPLE_SCHEMA)
        db.insert_many("t", [(i, f"row-{i}") for i in range(50)])

    with Database.open(path, page_size=PAGE_SIZE) as db:
        assert db.pager.recovery.ran is False
        assert len(db.rows("t")) == 50
    assert path.with_name(path.name + "-wal").stat().st_size == 0


def test_an_uncommitted_transaction_is_rolled_back_by_recovery(tmp_path: Path):
    """**The milestone, in one assertion.**

    Until Milestone 9 this test asserted the opposite, and said so: an
    uncommitted transaction that outgrew the buffer pool had pages *stolen* —
    written to disk before it committed — and with the undo log dying alongside
    the process, those rows were simply there afterwards.

    They are not there now. The stolen pages were logged before they were
    allowed onto the disk, so recovery can see them, and the absence of a commit
    record is what tells it to put them back.
    """
    path = tmp_path / "rolled-back.chendb"
    crash_mid_transaction(path)

    rows, report = rows_after_recovery(path)
    assert [row for row in rows if row[0] >= 1000] == [], (
        "the uncommitted rows must be gone — this is what the log is for"
    )
    assert len(rows) == 20, "and the committed ones must all still be here"
    assert report.pages_undone > 0, "pages really were stolen, and really were undone"


def test_a_committed_transaction_survives_without_a_sync(tmp_path: Path):
    """No-force, asserted.

    The child commits and is killed immediately — no ``sync()``, no ``close()``.
    Every dirty page is still in the buffer pool when the process dies. The only
    thing on the disk is the log, and it is enough.
    """
    path = tmp_path / "committed.chendb"
    crash_after_committing(path)

    rows, report = rows_after_recovery(path)
    assert len(rows) == 220, "the commit record is what makes these durable"
    assert report.pages_undone == 0, "nothing to undo — the transaction finished"
    assert report.losers == ()


def test_recovery_is_idempotent(tmp_path: Path):
    """Recovering twice must land in the same place as recovering once.

    A machine that crashed can crash again while recovering, so this is not a
    hypothetical. Redo is conditional on the page's own LSN and undo logs its
    compensations, which together mean a second pass finds nothing left to do.
    """
    path = tmp_path / "twice.chendb"
    crash_mid_transaction(path)

    first, first_report = rows_after_recovery(path)
    second, second_report = rows_after_recovery(path)

    assert first == second
    assert first_report.ran and not second_report.ran, (
        "the first open consumed the log; the second found a clean database"
    )


def test_a_torn_log_tail_is_ignored_rather_than_fatal(tmp_path: Path):
    """A half-written record at the end of a log is the normal state after a
    crash — the process died part-way through a write. It must not be read as
    corruption.
    """
    path = tmp_path / "torn-log.chendb"
    crash_mid_transaction(path)

    wal = path.with_name(path.name + "-wal")
    raw = wal.read_bytes()
    assert raw, "the crash left records"
    wal.write_bytes(raw[: -len(raw) // 3])  # lop off the tail mid-record

    rows, report = rows_after_recovery(path)
    assert report.ran
    assert report.truncated_tail, "the partial record was noticed, not decoded"
    assert [row for row in rows if row[0] >= 1000] == []


def test_a_checkpoint_empties_the_log(tmp_path: Path):
    path = tmp_path / "checkpointed.chendb"
    wal = path.with_name(path.name + "-wal")

    with Database.open(path, page_size=PAGE_SIZE) as db:
        db.create_table("t", SIMPLE_SCHEMA)
        db.insert_many("t", [(i, f"row-{i}") for i in range(200)])
        assert wal.stat().st_size > 0, "the log has been accumulating"

        db.checkpoint()
        assert wal.stat().st_size == 0, "and the checkpoint discarded it"
        assert len(db.rows("t")) == 200, "without losing anything"


def test_work_after_a_checkpoint_still_survives_a_crash(tmp_path: Path):
    """The LSN base has to keep the stream monotonic across a truncation.

    A checkpoint resets the log *file* to zero bytes, but not the LSN *stream* —
    ``checkpoint_lsn`` in the meta page carries the difference. Get that wrong
    and records written after a checkpoint carry LSNs below the ones already
    stamped on pages, so redo compares them and skips work it needed to do. The
    rows inserted after the checkpoint here are what would go missing.
    """
    path = tmp_path / "post-checkpoint.chendb"
    crash_after_checkpoint(path)

    rows, report = rows_after_recovery(path)
    assert report.ran
    assert len(rows) == 60, "30 before the checkpoint, 30 committed after it"


def test_a_database_created_and_killed_immediately_is_recoverable(tmp_path: Path):
    """The bug the -1 sentinel fixes, as a crash rather than a unit test.

    A brand-new database's very first log record is the meta page, at LSN 0.
    Kill the process before that page reaches the disk and there is nothing to
    read — and if "no page there" answered 0 rather than -1, redo would compare
    0 >= 0, decide the page was already current, and skip the only page that
    makes the file a database.
    """
    path = tmp_path / "newborn.chendb"
    source = """
import os, signal, sys
sys.path.insert(0, {repo!r})
from engine import Database
from engine.executor.engine import execute_script

db = Database.open({path!r}, page_size={page_size})
# Through SQL, so each statement gets an implicit transaction and therefore a
# commit record. A bare db.insert() is not committed and not durable, which is
# what engine/database.py's docstring says and what this deliberately avoids
# testing here.
execute_script("CREATE TABLE t (id INTEGER NOT NULL);", db)
execute_script("INSERT INTO t VALUES (1);", db)
sys.stdout.write("ready")
sys.stdout.flush()
os.kill(os.getpid(), signal.SIGKILL)
"""
    _crash_with(source, path)

    with Database.open(path, page_size=PAGE_SIZE) as db:
        assert db.table("t") is not None
        assert db.rows("t") == [(1,)]
