#!/usr/bin/env python3
"""Measure what diagnostics cost.

    python benchmarks/trace_overhead.py

Two questions, both answered with numbers rather than assertions:

1. **How much does each trace level cost?** The same workload runs at every
   level against a fresh database.
2. **Does the guarded-emit pattern actually matter?** The engine writes

       if tracer.storage:
           tracer.emit(PageReadEvent(...))

   rather than calling ``emit`` unguarded. Python evaluates arguments before
   the call, so the unguarded form constructs the event object even when it is
   about to be discarded. This measures the difference directly.

Absolute times depend on the machine and the filesystem. The ratios are the
interesting part, and they are what a regression would show up in.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.diagnostics import (
    NullSink,
    PageReadEvent,
    RingBufferSink,
    TraceLevel,
    Tracer,
)

ROW_COUNT = 2_000
REPEATS = 3
PAGE_SIZE = 4096
EMIT_ITERATIONS = 200_000

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("age", DataType.INTEGER),
    Column("active", DataType.BOOLEAN),
)


def workload(db: Database) -> int:
    """Insert, scan, point-read, delete, scan. One of each traced operation."""
    record_ids = db.insert_many(
        [(i, f"user{i}@example.com", i % 90, i % 2 == 0) for i in range(ROW_COUNT)]
    )
    rows = sum(1 for _ in db.scan())
    for record_id in record_ids[:100]:
        db.get(record_id)
    for record_id in record_ids[::50]:
        db.delete(record_id)
    rows += sum(1 for _ in db.scan())
    return rows


def time_level(level: TraceLevel, workdir: Path, run: int) -> tuple[float, int, int]:
    """Run the workload once. Returns (seconds, events recorded, rows produced)."""
    sink = RingBufferSink(capacity=10_000_000)
    tracer = Tracer(sink, level)
    path = workdir / f"{level.name.lower()}-{run}.chendb"

    with Database.open(path, page_size=PAGE_SIZE, tracer=tracer) as db:
        db.create_table("users", SCHEMA)
        started = time.perf_counter()
        rows = workload(db)
        elapsed = time.perf_counter() - started

    return elapsed, sink.stats.total_recorded, rows


def benchmark_levels(workdir: Path) -> None:
    print(f"\n\033[1mTrace level overhead\033[0m  ({ROW_COUNT:,} rows, best of {REPEATS})")
    print("─" * 74)
    print(f"{'level':<10} {'seconds':>9} {'vs OFF':>9} {'events':>10} {'µs/event':>10}")
    print("─" * 74)

    baseline: float | None = None
    row_counts: set[int] = set()
    digests: set[bytes] = set()

    for level in TraceLevel:
        timings: list[float] = []
        events = 0
        for run in range(REPEATS):
            elapsed, events, rows = time_level(level, workdir, run)
            timings.append(elapsed)
            row_counts.add(rows)
        best = min(timings)
        if baseline is None:
            baseline = best

        ratio = best / baseline
        # Below a few hundred events the delta is smaller than timing noise,
        # so reporting a per-event cost would be inventing precision.
        if events >= 500:
            per_event = f"{(best - baseline) / events * 1e6:.3f}"
        else:
            per_event = "—"
        print(f"{level.name:<10} {best:>9.4f} {ratio:>8.2f}× {events:>10,} {per_event:>10}")

        digests.add((workdir / f"{level.name.lower()}-0.chendb").read_bytes())

    print("─" * 74)
    if len(row_counts) == 1:
        print(f"✓ every level produced the same {row_counts.pop():,} rows")
    else:
        print(f"✗ RESULTS DIFFERED between levels: {sorted(row_counts)}")
    if len(digests) == 1:
        print("✓ every level produced a byte-identical database file")
    else:
        print(f"✗ FILES DIFFERED between levels ({len(digests)} distinct)")


def benchmark_emit_pattern() -> None:
    """Guarded vs unguarded emit, with tracing off."""
    print(f"\n\033[1mEmit pattern with tracing OFF\033[0m  ({EMIT_ITERATIONS:,} calls)")
    print("─" * 74)

    tracer = Tracer(NullSink(), TraceLevel.OFF)

    def guarded() -> None:
        for page_id in range(EMIT_ITERATIONS):
            if tracer.storage:
                tracer.emit(  # pragma: no cover - never taken at OFF
                    PageReadEvent(page_id, page_id * PAGE_SIZE, "disk", 100)
                )

    def unguarded() -> None:
        for page_id in range(EMIT_ITERATIONS):
            # The event object is built before emit() can reject it.
            tracer.emit(PageReadEvent(page_id, page_id * PAGE_SIZE, "disk", 100))

    def measure(fn) -> float:
        return min(_time(fn) for _ in range(REPEATS))

    guarded_time = measure(guarded)
    unguarded_time = measure(unguarded)

    print(
        f"{'guarded':<12} {guarded_time:>9.4f}s  "
        f"{guarded_time / EMIT_ITERATIONS * 1e9:>7.1f} ns/call"
    )
    print(
        f"{'unguarded':<12} {unguarded_time:>9.4f}s  "
        f"{unguarded_time / EMIT_ITERATIONS * 1e9:>7.1f} ns/call"
    )
    print("─" * 74)
    print(
        f"The guard saves {unguarded_time / guarded_time:.1f}× per call site "
        f"when tracing is off."
    )
    print("That is why every emit in the engine is written behind one.")


def _time(fn) -> float:
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


def benchmark_storage_primitives(workdir: Path) -> None:
    """Per-operation costs with tracing off, so the engine is measured alone."""
    print("\n\033[1mStorage primitives (tracing OFF)\033[0m")
    print("─" * 74)

    path = workdir / "primitives.chendb"
    with Database.open(path, page_size=PAGE_SIZE) as db:
        db.create_table("users", SCHEMA)

        rows = [(i, f"user{i}@example.com", i % 90, i % 2 == 0) for i in range(ROW_COUNT)]
        insert_time = _time(lambda: db.insert_many(rows))

        reads_before = db.stats.page_reads
        scan_time = _time(lambda: sum(1 for _ in db.scan()))
        scan_reads = db.stats.page_reads - reads_before

        record_ids = [rid for rid, _ in db.scan()][:1000]
        point_time = _time(lambda: [db.get(rid) for rid in record_ids])

        sync_times = [_time(db.sync) for _ in range(20)]

        pages = db.page_count
        rows_per_page = ROW_COUNT / max(1, len(db.heap_page_ids()))

    print(f"{'insert':<22} {insert_time / ROW_COUNT * 1e6:>8.2f} µs/row")
    print(
        f"{'full scan':<22} {scan_time / ROW_COUNT * 1e6:>8.2f} µs/row"
        f"   ({scan_reads} page reads)"
    )
    print(
        f"{'point read by RecordId':<22} {point_time / len(record_ids) * 1e6:>8.2f} µs/row"
    )
    print(f"{'fsync':<22} {statistics.median(sync_times) * 1e6:>8.2f} µs   (median of 20)")
    print("─" * 74)
    print(
        f"{ROW_COUNT:,} rows in {pages} pages, ~{rows_per_page:.0f} rows/page "
        f"at {PAGE_SIZE} B/page."
    )
    print("Every page read above is a syscall. Milestone 7's buffer pool is")
    print("the first change that should move the scan and point-read numbers.")


def main() -> int:
    print("ChenDB — diagnostics and storage benchmark")
    print(f"Python {sys.version.split()[0]}  ·  page size {PAGE_SIZE} B")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        benchmark_levels(workdir)
        benchmark_emit_pattern()
        benchmark_storage_primitives(workdir)

    print("\nNumbers are machine-specific; the ratios are what to watch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
