#!/usr/bin/env python3
"""A narrated tour of the Milestone 9 write-ahead log.

    python examples/milestone9_wal.py

Seven things: what a commit actually writes, what that costs, why the log is not
two hundred times the size of the data, what a real crash leaves behind, what
recovery does about it in three passes, what a checkpoint is for, and where the
remaining limits are.

The crash is a **real** one (a child process killed with SIGKILL, no cleanup
handlers, no fsync on the way out) because any cooperative shutdown would
quietly flush the very buffers whose loss is the point.
"""

from __future__ import annotations

import signal
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.storage.pager import Pager
from engine.wal.record import RecordType

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)

PAGE_SIZE = 4096
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The crash demonstration uses small pages and a tiny pool on purpose. With the
#: defaults the whole workload is resident, nothing is ever evicted, and an
#: uncommitted transaction never reaches the disk at all, which is a true
#: outcome and a boring one. Forcing the pool to *steal* is what makes recovery
#: have something to undo.
CRASH_PAGE_SIZE = 512
CRASH_POOL_FRAMES = 4


def rule(title: str) -> None:
    print(f"\n{'-' * 78}\n{title}\n")


def median_ns(fn, calls: int = 300) -> float:
    samples = []
    for _ in range(calls):
        started = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - started)
    return statistics.median(samples)


def build(path: Path, *, wal: bool = True, rows: int = 2_000) -> Database:
    db = Database(Pager(path, page_size=PAGE_SIZE, wal=wal), create=True)
    db.create_table("t", SCHEMA)
    db.insert_many("t", [(n, f"row{n:05d}") for n in range(rows)])
    db.sync()
    return db


CRASH_CHILD = """
import os, signal, sys
sys.path.insert(0, {repo!r})
from engine import Column, DataType, Database, Schema

schema = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)
db = Database.open({path!r}, page_size={page_size}, buffer_pool_frames={frames})
db.create_table("t", schema)
db.insert_many("t", [(i, f"seed-{{i}}") for i in range(20)])
db.sync()

# Committed, and then killed. No sync, no close: every one of these pages is
# still in the buffer pool. The only thing on the disk is the log.
db.begin()
db.insert_many("t", [(1000 + i, f"committed-{{i}}") for i in range(500)])
db.commit()

# And an open transaction that never commits.
db.begin()
db.insert_many("t", [(9000 + i, f"doomed-{{i}}") for i in range(500)])

sys.stdout.write("ready")
sys.stdout.flush()
os.kill(os.getpid(), signal.SIGKILL)
"""


def crash(path: Path) -> None:
    source = CRASH_CHILD.format(
        repo=str(REPO_ROOT),
        path=str(path),
        page_size=CRASH_PAGE_SIZE,
        frames=CRASH_POOL_FRAMES,
    )
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.stdout == "ready", done.stderr
    assert done.returncode == -signal.SIGKILL


def main() -> int:
    print("ChenDB Milestone 9 - the write-ahead log")

    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)

        # -- 1 -------------------------------------------------------------
        rule("1. What a commit writes")

        db = build(root / "one.chendb")
        db.checkpoint()
        log = db.pager.wal
        assert log is not None

        db.begin()
        db.insert("t", (500_001, "a"))
        db.insert("t", (500_002, "b"))
        db.commit()
        log.flush()

        print("  BEGIN; INSERT; INSERT; COMMIT  ->")
        for record in log.read_all()[0]:
            page = (
                f"page {record.page_id}" if record.record_type is RecordType.UPDATE else ""
            )
            undo = " +undo" if record.has_undo else ""
            print(
                f"    lsn {record.lsn:>7}  {record.record_type.name.lower():<10}"
                f" txn {record.transaction_id}  {page:<8}{record.size:>6} B{undo}"
            )
        print("\n  The commit record is 44 bytes and it is the only one that is")
        print("  fsynced. The pages it refers to are still in the buffer pool.")
        db.close()

        # -- 2 -------------------------------------------------------------
        rule("2. What the log costs a write, and a commit")

        for wal in (False, True):
            handle = build(root / f"cost-{wal}.chendb", wal=wal)
            counter = [10**6]

            def one_insert(handle=handle, counter=counter) -> None:
                counter[0] += 1
                handle.insert("t", (counter[0], "x"))

            print(
                f"  insert, log {'on ' if wal else 'off'}   {median_ns(one_insert):>8,.0f} ns"
            )
            handle.close()

        db = build(root / "commit.chendb")
        log = db.pager.wal
        assert log is not None
        counter = [2 * 10**6]

        def one_commit() -> None:
            counter[0] += 1
            db.begin()
            db.insert("t", (counter[0], "y"))
            db.commit()

        with_sync = median_ns(one_commit, 200)
        log.set_sync_policy(sync_on_commit=False)
        without = median_ns(one_commit, 200)

        print(
            f"\n  commit, fsync on    {with_sync / 1000:>8.1f} us"
            f"  -> {1e9 / with_sync:>8,.0f} commits/s"
        )
        print(
            f"  commit, fsync off   {without / 1000:>8.1f} us"
            f"  -> {1e9 / without:>8,.0f} commits/s"
        )
        print(f"  the fsync itself    {(with_sync - without) / 1000:>8.1f} us")
        print("\n  That ceiling says nothing about how much work each transaction")
        print("  did. It is the disk, once per commit - which is what group commit")
        print("  exists to amortise, and why it needs more than one writer.")
        db.close()

        # -- 3 -------------------------------------------------------------
        rule("3. Why the log is 5x the data and not 200x")

        db = build(root / "volume.chendb")
        db.checkpoint()
        log = db.pager.wal
        assert log is not None

        db.begin()
        db.insert_many("t", [(3 * 10**6 + n, "z") for n in range(20_000)])
        db.commit()
        log.flush()

        size = log.path.stat().st_size
        data = db.page_count * PAGE_SIZE
        stats = log.stats
        total = stats.records_appended + stats.records_coalesced
        print("  20,000 rows in one transaction\n")
        print(f"    log             {size / 1024 / 1024:>8.2f} MiB")
        print(f"    pages           {data / 1024:>8.0f} KiB")
        print(f"    amplification   {size / data:>8.1f}x")
        print(f"    records written {stats.records_appended:>8,}")
        print(
            f"    coalesced away  {stats.records_coalesced:>8,}"
            f"   ({stats.records_coalesced / total * 100:.0f}%)"
        )
        print("\n  Writing the same page twice in a row makes two records of which")
        print("  only the second matters. If the first is still staged - not yet")
        print("  written - the second replaces it. Without that this log is 81 MiB.")
        db.close()

        # -- 4 -------------------------------------------------------------
        rule("4. A checkpoint")

        db = build(root / "checkpoint.chendb", rows=20_000)
        # build() ends with sync(), which already flushed the pool. Dirty some
        # pages again, or the checkpoint has nothing to write and reports zero.
        db.insert_many("t", [(4 * 10**6 + n, "k") for n in range(3_000)])
        log = db.pager.wal
        assert log is not None
        before = log.path.stat().st_size
        started = time.perf_counter_ns()
        pages = db.checkpoint()
        elapsed = time.perf_counter_ns() - started

        print(f"  log {before / 1024:>8,.0f} KiB  ->  {log.path.stat().st_size} B")
        print(f"  {pages} page(s) flushed in {elapsed / 1e6:.1f} ms")
        print(f"  the stream continues at LSN {log.base_lsn:,}, not at 0 -")
        print("  which is what checkpoint_lsn in the meta page is for.")
        db.close()

        # -- 5 -------------------------------------------------------------
        rule("5. A real crash")

        path = root / "crashed.chendb"
        crash(path)
        wal_size = path.with_name(path.name + "-wal").stat().st_size
        print("  A child process: 20 rows synced, 500 committed, 500 left open,")
        print("  then SIGKILL. No close(), no fsync, no cleanup handlers.")
        print(
            f"  {CRASH_PAGE_SIZE}-byte pages and a {CRASH_POOL_FRAMES}-frame pool, so the pool"
        )
        print("  is forced to steal - some of those uncommitted pages are on disk.\n")
        print(f"    database file  {path.stat().st_size / 1024:>8,.0f} KiB")
        print(f"    log file       {wal_size / 1024:>8,.0f} KiB")
        print("\n  The 500 committed rows may never have reached the database file.")
        print("  Their commit record did.")

        # -- 6 -------------------------------------------------------------
        rule("6. Recovery, in three passes")

        started = time.perf_counter_ns()
        db = Database.open(path, page_size=CRASH_PAGE_SIZE)
        elapsed = time.perf_counter_ns() - started
        report = db.pager.recovery

        print(f"    analysis   {report.records_scanned:>5,} record(s) scanned")
        print(
            f"               finished {list(report.winners)}, "
            f"interrupted {list(report.losers)}"
        )
        print(
            f"    redo       {report.pages_redone:>5,} replayed, "
            f"{report.pages_skipped:,} already current"
        )
        print(f"    undo       {report.pages_undone:>5,} put back")
        print(f"\n  {elapsed / 1e6:.1f} ms total. Rows now: {db.count('t'):,}")

        committed = sum(1 for row in db.rows("t") if 1000 <= row[0] < 2000)
        doomed = sum(1 for row in db.rows("t") if row[0] >= 9000)
        print(f"\n    committed rows recovered   {committed:>5}  (of 500)")
        print(f"    uncommitted rows surviving {doomed:>5}  (of 500)")
        print("\n  Redo replays the interrupted transaction's work too, then undo")
        print("  takes it back. ARIES calls that repeating history: recovery cannot")
        print("  know which of a loser's pages reached the disk, so it puts the")
        print("  database in exactly the state the crash left it and rolls back")
        print("  from there - using the same undo path a live ROLLBACK uses.")
        db.close()

        # -- 7 -------------------------------------------------------------
        rule("7. What is still missing")

        print("  Whole-page records.  A real system logs the operation - 'insert")
        print("  this tuple into page 7' - and pays for a redo routine per")
        print("  operation. PostgreSQL splits it: a full page the first time each")
        print("  page changes after a checkpoint, deltas after.")
        print()
        print("  Sharp checkpoints.  This one stops the world and flushes")
        print("  everything, which is why recovery never has to rebuild a")
        print("  dirty-page table. It is also why it would not survive a")
        print("  hundred-gigabyte pool. Real systems checkpoint fuzzily.")
        print()
        print("  No group commit.  One fsync per commit, because with one writer")
        print("  there is nobody to share a flush with. Milestone 10 changes that.")
        print()
        print("  Page granularity.  Two transactions writing different rows of the")
        print("  same page conflict at page level. Invisible with one writer, and")
        print("  the first thing a second one hits - which is MVCC's problem.")

    print(f"\n{'-' * 78}")
    print("docs/milestone-09-wal.md has the full reasoning.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
