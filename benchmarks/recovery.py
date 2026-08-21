#!/usr/bin/env python3
"""Measure what a crash costs to recover from, and what a checkpoint buys.

    python benchmarks/recovery.py

Recovery is the one code path whose runtime nobody measures until the day it
matters, and it is the path a database is *for*. Three numbers, and the third is
the one that decides the first two:

  * **recovery time against log length**, split into the three ARIES passes;
  * **redone against skipped**, because a record whose page already carries a
    higher LSN is work the last checkpoint saved;
  * **the checkpoint itself**, which is what truncates the log and therefore what
    bounds recovery.

The crash here is ``Database.abandon()``: no rollback, no flush, no checkpoint,
the file left exactly as a killed process would leave it. That is a weaker crash
than ``tests/recovery/`` uses, which kills a real child process with ``SIGKILL``
so no Python-level cleanup can run at all. This one is in-process so it can be
timed; the tests are the ones that prove it.

Absolute times depend on the machine and on how fast it can ``fsync``.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema

PAGE_SIZE = 4096
SIZES = (100, 1_000, 5_000)

#: A deliberately small pool, and it is not a trick. Recovery only has work to
#: do if some of the crashed transaction's pages reached the disk, and the only
#: thing that puts an uncommitted page there is an eviction. With a pool big
#: enough to hold the whole table, nothing is evicted, the log is never forced,
#: and the crash loses everything cleanly with nothing to replay: a real
#: outcome, and an uninformative benchmark. Four frames is what a large
#: transaction on a real database looks like from the pool's point of view.
POOL_FRAMES = 4

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)


def crash_with(path: Path, rows: int, *, commit: bool) -> None:
    """Write ``rows`` rows, then die without cleaning up.

    ``commit=True`` makes the transaction a *winner*: recovery must replay it,
    because a commit record is on the disk while the pages may not be. With
    ``commit=False`` it is a loser and every page it touched must be undone.
    """
    db = Database.open(path, page_size=PAGE_SIZE, buffer_pool_frames=POOL_FRAMES)
    db.create_table("users", SCHEMA)
    db.checkpoint()
    db.begin()
    for n in range(rows):
        db.insert("users", (n, f"row-{n:06d}"))
    if commit:
        db.commit()
    db.abandon()


def recover(path: Path) -> tuple[float, object]:
    """Open the file, which runs recovery, and report what it did."""
    started = time.perf_counter_ns()
    db = Database.open(path)
    elapsed = (time.perf_counter_ns() - started) / 1e6
    report = db.recovery
    rows = db.count("users")
    db.close()
    return elapsed, (report, rows)


def header(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def main() -> int:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        print(
            f"ChenDB recovery benchmark - {PAGE_SIZE}-byte pages\n"
            "Each case crashes a fresh file with Database.abandon(), then reopens it."
        )

        header("An interrupted transaction: analysis, redo, undo")
        print(
            f"  {'rows lost':>10}{'records':>9}{'redone':>8}{'skipped':>9}"
            f"{'undone':>8}{'recover':>11}{'rows after':>12}"
        )
        for rows in SIZES:
            path = root / f"loser{rows}.chendb"
            crash_with(path, rows, commit=False)
            elapsed, (report, after) = recover(path)
            print(
                f"  {rows:>10,}{report.records_scanned:>9,}{report.pages_redone:>8,}"
                f"{report.pages_skipped:>9,}{report.pages_undone:>8,}"
                f"{elapsed:>8.1f} ms{after:>12,}"
            )
        print(
            "\n  Zero rows survive every time, which is the outcome, but the two\n"
            "  ways of getting there are worth separating. The smallest case never\n"
            "  filled the pool, so no page was evicted, the log was never forced,\n"
            "  and the crash left nothing to replay or undo: correct by having\n"
            "  written nothing. The larger ones did evict, so recovery replays the\n"
            "  loser's own changes (ARIES repeats history) and then takes them\n"
            "  back page by page.\n\n"
            "  The skipped column dominates because a redo record is only applied\n"
            "  when the page's own LSN is below it: most of these pages already\n"
            "  carry the change, and the check is what makes recovery safe to\n"
            "  interrupt and run again."
        )

        header("A committed transaction the crash caught before the pages landed")
        print(
            f"  {'rows':>10}{'records':>9}{'redone':>8}{'skipped':>9}"
            f"{'undone':>8}{'recover':>11}{'rows after':>12}"
        )
        for rows in SIZES:
            path = root / f"winner{rows}.chendb"
            crash_with(path, rows, commit=True)
            elapsed, (report, after) = recover(path)
            print(
                f"  {rows:>10,}{report.records_scanned:>9,}{report.pages_redone:>8,}"
                f"{report.pages_skipped:>9,}{report.pages_undone:>8,}"
                f"{elapsed:>8.1f} ms{after:>12,}"
            )
        print(
            "\n  Every row is there. The commit record was fsynced, the pages were\n"
            "  not, and redo is what closes the gap: no-force is only safe because\n"
            "  this works. The skipped column is the pages that had already been\n"
            "  written by an eviction, and needed nothing."
        )

        header("Phases, on the largest case")
        path = root / "phases.chendb"
        crash_with(path, SIZES[-1], commit=False)
        _, (report, _) = recover(path)
        total = sum(report.phase_ns.values()) or 1
        for phase, spent in report.phase_ns.items():
            print(f"  {phase:<12}{spent / 1e6:>8.1f} ms{100 * spent / total:>8.0f}%")
        print(
            "\n  Analysis reads the log once and decides who committed. Redo reads\n"
            "  it again and writes pages. Undo walks the losers backwards, and\n"
            "  logs each restore before applying it, so a crash during recovery\n"
            "  leaves the finished part of the undo in the log."
        )

        header("What a checkpoint costs, and what it removes")
        path = root / "checkpoint.chendb"
        db = Database.open(path, page_size=PAGE_SIZE)
        db.create_table("users", SCHEMA)
        db.insert_many("users", [(n, f"row-{n:06d}") for n in range(SIZES[-1])])
        log = db.wal
        assert log is not None, "the log is disabled"
        before = log.path.stat().st_size
        started = time.perf_counter_ns()
        flushed = db.checkpoint()
        elapsed = (time.perf_counter_ns() - started) / 1e6
        print(
            f"  log before          {before / 1024 / 1024:>9.1f} MiB\n"
            f"  log after           {log.path.stat().st_size / 1024:>9.1f} KiB\n"
            f"  pages flushed       {flushed:>9,}\n"
            f"  checkpoint          {elapsed:>9.1f} ms"
        )
        db.close()
        print(
            "\n  A checkpoint here is *sharp*: it flushes every dirty page, then\n"
            "  truncates the log, so recovery never has to start earlier than the\n"
            "  last one. That is what keeps the recovery numbers above bounded by\n"
            "  checkpoint frequency rather than by database age, and it is the\n"
            "  simplification that lets analysis skip a dirty-page table.\n\n"
            "  The log's size before the checkpoint is the write amplification\n"
            "  this design pays: an update record carries a full before-image and\n"
            "  a full after-image of a 4 KiB page, so a few thousand rows of data\n"
            "  are tens of MiB of log until a checkpoint reclaims it."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
