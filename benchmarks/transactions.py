#!/usr/bin/env python3
"""Measure what a commit costs, and what a rollback costs.

    python benchmarks/transactions.py

A transaction's price has two halves that scale differently, and conflating them
is how "transactions are slow" becomes folklore instead of a number:

  * **per commit**, one ``fsync`` of the log, which does not care how much work
    the transaction did;
  * **per page touched**, an undo image and a log record, which is all the
    transaction did care about.

So the same 1,000 rows cost wildly different amounts depending on how many
transactions they are spread over, and the shape of that curve is the whole
argument for batching. The fsync is priced directly by turning it off:
``WriteAheadLog.set_sync_policy`` exists for this measurement and for the
visualizer, and nothing in the engine turns it off on its own.

Rollback is measured separately because it is the operation ChenDB pays for that
PostgreSQL does not: undo here restores pages, so its cost grows with pages
touched rather than being free.

Absolute times depend on the machine, and this one is dominated by how fast its
disk can ``fsync``. The ratios are the interesting part.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema

PAGE_SIZE = 4096
TOTAL_ROWS = 1_000
BATCHES = (1, 10, 100, 1_000)
REPEATS = 5

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)


def fresh(path: Path, *, sync: bool) -> Database:
    db = Database.open(path, page_size=PAGE_SIZE)
    db.create_table("users", SCHEMA)
    log = db.wal
    if log is not None:
        log.set_sync_policy(sync_on_commit=sync)
    return db


def insert_in_batches(db: Database, rows_per_txn: int) -> None:
    """Insert :data:`TOTAL_ROWS` rows, committing every ``rows_per_txn``."""
    written = 0
    while written < TOTAL_ROWS:
        db.begin()
        for n in range(written, min(written + rows_per_txn, TOTAL_ROWS)):
            db.insert("users", (n, f"row-{n:06d}"))
        db.commit()
        written += rows_per_txn


def median_of(fn) -> float:
    """Median of :data:`REPEATS` runs, in milliseconds.

    ``fn`` times itself and returns milliseconds, because opening and closing
    the file must stay *outside* the measurement: ``close`` runs a checkpoint,
    which fsyncs, and would put the very cost being priced back into the run
    that was supposed to be without it.
    """
    return statistics.median(fn() for _ in range(REPEATS))


def header(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def main() -> int:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        counter = iter(range(10_000))
        print(
            f"ChenDB transaction benchmark - {TOTAL_ROWS:,} rows per run, "
            f"{PAGE_SIZE}-byte pages\n"
            f"Medians of {REPEATS} runs, each against a new file."
        )

        header("The same rows, spread over more or fewer transactions")
        print(
            f"  {'rows per txn':<14}{'commits':>9}{'fsyncs':>9}"
            f"{'fsync on':>13}{'fsync off':>13}{'difference':>13}"
        )
        for rows_per_txn in BATCHES:
            commits = -(-TOTAL_ROWS // rows_per_txn)

            def run(sync: bool, rows_per_txn: int = rows_per_txn) -> float:
                db = fresh(root / f"t{next(counter)}.chendb", sync=sync)
                started = time.perf_counter_ns()
                insert_in_batches(db, rows_per_txn)
                elapsed = (time.perf_counter_ns() - started) / 1e6
                db.close()
                return elapsed

            on = median_of(lambda rows_per_txn=rows_per_txn: run(True, rows_per_txn))
            off = median_of(lambda rows_per_txn=rows_per_txn: run(False, rows_per_txn))
            syncs = _sync_count(root / f"t{next(counter)}.chendb", rows_per_txn)
            print(
                f"  {rows_per_txn:<14,}{commits:>9,}{syncs:>9,}{on:>10.1f} ms"
                f"{off:>10.1f} ms{on - off:>10.1f} ms"
            )
        print(
            "\n  Row-at-a-time commits pay the fsync a thousand times for the same\n"
            "  thousand rows, and that column is where the difference is visible.\n"
            "  Batching does not make the writes cheaper, it makes the *durability\n"
            "  points* fewer, which is the only thing that was ever expensive. By\n"
            "  100 rows per transaction the fsync has stopped being the story and\n"
            "  the difference is inside this machine's run-to-run noise, which is\n"
            "  worth printing rather than rounding into a claim.\n\n"
            "  Turning the fsync off is a measurement, not an option: it leaves a\n"
            "  commit durable against a process crash and not against a machine\n"
            "  one. Nothing in the engine does this on its own."
        )

        header("What one fsync costs on this machine")
        path = root / "fsync.chendb"
        db = fresh(path, sync=True)
        insert_in_batches(db, 1)
        log = db.wal
        assert log is not None, "the log is disabled"
        stats = log.stats
        per_sync = stats.sync_ns / stats.syncs / 1000 if stats.syncs else 0.0
        print(
            f"  {stats.syncs:,} fsyncs, {stats.sync_ns / 1e6:.1f} ms total, "
            f"{per_sync:.0f} us each"
        )
        print(
            f"  {stats.records_appended:,} log records, "
            f"{stats.bytes_appended / 1024 / 1024:.1f} MiB appended, "
            f"{stats.records_coalesced:,} coalesced away"
        )
        db.close()
        print(
            "\n  A coalesced record is a second change to a page that was already\n"
            "  staged, replaced rather than followed. Every one is a page image\n"
            "  that never reached the log."
        )

        header("Rollback: the cost of undoing pages rather than nothing")
        print(f"  {'rows':<10}{'insert':>12}{'rollback':>12}{'pages undone':>14}")
        for rows in (10, 100, 1_000):
            path = root / f"r{next(counter)}.chendb"
            db = fresh(path, sync=True)
            db.begin()
            insert_ms = timed_once(
                lambda rows=rows, db=db: [
                    db.insert("users", (n, f"row-{n:06d}")) for n in range(rows)
                ]
            )
            held = db.active_transaction
            undo_pages = held.undo.page_count if held is not None else 0
            rollback_ms = timed_once(db.rollback)
            print(
                f"  {rows:<10,}{insert_ms:>9.1f} ms{rollback_ms:>9.1f} ms{undo_pages:>14,}"
            )
            db.close()
        print(
            "\n  Undo is physical: the log holds a before-image per page, not per\n"
            "  row, so a rollback restores pages and its cost tracks the page\n"
            "  count rather than the row count. PostgreSQL's rollback is free\n"
            "  because it never undoes anything, and pays for that with a commit\n"
            "  log and vacuum instead. The bill arrives somewhere either way."
        )

    return 0


def _sync_count(path: Path, rows_per_txn: int) -> int:
    """How many fsyncs the run actually performed, counted by the log itself."""
    db = fresh(path, sync=True)
    insert_in_batches(db, rows_per_txn)
    log = db.wal
    syncs = log.stats.syncs if log is not None else 0
    db.close()
    return syncs


def timed_once(fn) -> float:
    """One run, in milliseconds. Used where the work cannot be repeated."""
    started = time.perf_counter_ns()
    fn()
    return (time.perf_counter_ns() - started) / 1e6


if __name__ == "__main__":
    raise SystemExit(main())
