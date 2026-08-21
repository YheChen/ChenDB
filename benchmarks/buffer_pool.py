#!/usr/bin/env python3
"""Measure what the buffer pool does, and when it does nothing at all.

    python benchmarks/buffer_pool.py

A page cache is judged by one number, the hit rate, and that number is not a
property of the cache. It is a property of the cache *and the workload*, and the
interesting result is not "bigger is better" but the two shapes below:

  * a working set that fits gets almost every read from memory;
  * a scan larger than the pool gets **nothing**, and evicts what was there.

The second is *sequential flooding*, and it is why PostgreSQL confines a large
scan to a ring buffer instead of letting it walk the whole cache. This measures
both against a real file, and prints the eviction counters next to the hit rate
so a zero can be told from a cache that was never asked.

Absolute times depend on the machine. The hit rates do not, and neither does
the write-back ratio.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.storage.heap import RecordId

#: 256-byte pages, so a few hundred rows make a table larger than any pool this
#: benchmark uses. At 4 KiB the whole thing would be resident and every
#: measurement below would read 100%, which is a true number about nothing.
PAGE_SIZE = 256
ROW_COUNT = 600
REPEATS = 3

#: Pool sizes to sweep. The table is about 75 pages, so 8 frames is a pool that
#: cannot hold it and 128 is one that can.
POOL_SIZES = (4, 8, 16, 32, 128)

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)


def prepare(path: Path) -> tuple[list[RecordId], int]:
    """Build the file once. Returns record ids and the table's page count."""
    db = Database.open(path, page_size=PAGE_SIZE)
    db.create_table("users", SCHEMA)
    db.insert_many("users", [(n, f"row-{n:04d}") for n in range(ROW_COUNT)])
    db.sync()
    record_ids = [record_id for record_id, _ in db.scan("users")]
    pages = len(db.heap_page_ids("users"))
    db.close()
    return record_ids, pages


def timed(fn) -> float:
    """Median wall time in milliseconds over :data:`REPEATS` runs."""
    samples = []
    for _ in range(REPEATS):
        started = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - started) / 1e6)
    return statistics.median(samples)


def header(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def working_set(db: Database, record_ids: list[RecordId], span: int, rounds: int) -> None:
    """Read the same few rows over and over. The case a cache exists for."""
    subset = record_ids[:span]
    for _ in range(rounds):
        for record_id in subset:
            db.get("users", record_id)


def flooding(db: Database, rounds: int) -> None:
    """Scan the whole table repeatedly. The case a cache cannot help with."""
    for _ in range(rounds):
        for _ in db.scan("users"):
            pass


def report(db: Database, label: str, frames: int) -> None:
    stats = db.pager.buffer_pool.stats
    pager = db.stats
    print(
        f"  {label:<22}{frames:>7}{stats.hit_rate * 100:>9.1f}%"
        f"{stats.hits:>9}{stats.misses:>9}{stats.evictions:>11}"
        f"{pager.physical_reads:>10}"
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        path = root / "pool.chendb"
        record_ids, table_pages = prepare(path)
        print(
            f"ChenDB buffer pool benchmark - {ROW_COUNT:,} rows in "
            f"{table_pages} pages of {PAGE_SIZE} B "
            f"({ROW_COUNT / table_pages:.0f} rows per page)\n"
            f"Medians of {REPEATS} runs; each configuration reopens the file, so "
            f"the counters start at zero."
        )

        header("Hit rate against pool size, two workloads")
        print(
            f"  {'workload':<22}{'frames':>7}{'hit rate':>10}"
            f"{'hits':>9}{'misses':>9}{'evictions':>11}{'preads':>10}"
        )
        for frames in POOL_SIZES:
            db = Database.open(path, buffer_pool_frames=frames)
            working_set(db, record_ids, span=40, rounds=8)
            report(db, "working set, 6 pages", frames)
            db.close()

            db = Database.open(path, buffer_pool_frames=frames)
            flooding(db, rounds=4)
            report(db, "full scan, all pages", frames)
            db.close()

        print(
            "\n  The working set is 40 rows, about six pages, so every pool here\n"
            "  holds it and the misses are the first pass. The full scan is\n"
            f"  {table_pages} pages: below that size the pool returns almost\n"
            "  nothing and evicts on every read, which is sequential flooding."
        )

        header("What a hit is worth")
        # 400 point reads over the same 40 rows, once through a pool that holds
        # them and once through a pool of two frames, which cannot. Both after a
        # warm-up pass, so neither pays for opening the file.
        print(f"  {'pool':<22}{'us/read':>10}{'hit rate':>10}{'preads':>9}")
        for frames in (64, 2):
            db = Database.open(path, buffer_pool_frames=frames)
            working_set(db, record_ids, span=40, rounds=1)
            millis = timed(lambda db=db: working_set(db, record_ids, span=40, rounds=10))
            stats = db.pager.buffer_pool.stats
            reads = db.stats.physical_reads
            label = "holds the working set" if frames > 8 else "two frames"
            print(
                f"  {label:<22}{millis * 1000 / 400:>10.2f}"
                f"{stats.hit_rate * 100:>9.1f}%{reads:>9}"
            )
            db.close()
        print(
            "\n  The syscall count is the result: the same answers cost either a\n"
            "  couple of hundred preads or a handful. The *time* barely moves,\n"
            "  and that is the honest version of this benchmark. A pread of a page the\n"
            "  OS already has cached is microseconds, while decoding a row is\n"
            "  interpreted Python, so I/O is not what this engine spends its time\n"
            "  on. It is why engine/optimizer/cost.py charges a tuple about a\n"
            "  seventh of a page rather than PostgreSQL's hundredth, and why a\n"
            "  warm full scan through a large pool is not measurably faster than\n"
            "  one through a small one here."
        )

        header("Write-back: logical writes against syscalls")
        db = Database.open(root / "writes.chendb", page_size=PAGE_SIZE)
        db.create_table("users", SCHEMA)
        db.insert_many("users", [(n, f"row-{n:04d}") for n in range(ROW_COUNT)])
        logical = db.stats.page_writes
        physical = db.stats.physical_writes
        absorbed = db.pager.buffer_pool.stats.writes_absorbed
        db.close()
        share = 100 * (1 - physical / logical) if logical else 0.0
        print(
            f"  {ROW_COUNT:,}-row insert: {logical:,} logical writes -> "
            f"{physical:,} syscalls   ({share:.0f}% absorbed)"
        )
        print(
            f"  {absorbed:,} of them hit a frame that was already dirty, so the "
            "previous\n  version was never written at all."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
