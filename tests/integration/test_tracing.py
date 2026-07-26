"""Tracing must be observable, bounded, and invisible to results."""

from __future__ import annotations

import time
from pathlib import Path

from engine.database import Database
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.diagnostics.levels import EventCategory
from engine.serialization.schema import Schema

PAGE_SIZE = 256
ROW_COUNT = 200


def build(path: Path, schema: Schema, level: TraceLevel) -> tuple[Database, RingBufferSink]:
    sink = RingBufferSink(capacity=1_000_000)
    db = Database.open(path, page_size=PAGE_SIZE, tracer=Tracer(sink, level))
    db.create_table("users", schema)
    return db, sink


def workload(db: Database) -> list[tuple]:
    """Insert, point-read, delete, scan — one of each traced operation."""
    record_ids = db.insert_many("users",
        [(i, f"user-{i}", i % 90, i % 2 == 0, i / 3) for i in range(ROW_COUNT)]
    )
    for record_id in record_ids[:10]:
        db.get("users", record_id)  # the only source of VERBOSE-level RecordReadEvents
    for record_id in record_ids[::7]:
        db.delete("users", record_id)
    return db.rows("users")


def test_results_are_identical_at_every_trace_level(
    tmp_path: Path, users_schema: Schema
):
    baseline: list[tuple] | None = None
    for level in TraceLevel:
        path = tmp_path / f"{level.name.lower()}.chendb"
        db, _ = build(path, users_schema, level)
        with db:
            rows = workload(db)
        if baseline is None:
            baseline = rows
        else:
            assert rows == baseline, f"{level.name} changed the query result"
    assert baseline is not None and len(baseline) > 0


def test_the_files_produced_are_byte_identical_at_every_trace_level(
    tmp_path: Path, users_schema: Schema
):
    # Stronger than comparing rows: tracing must not perturb physical layout.
    digests: set[bytes] = set()
    for level in (TraceLevel.OFF, TraceLevel.STORAGE, TraceLevel.VERBOSE):
        path = tmp_path / f"bytes-{level.name.lower()}.chendb"
        db, _ = build(path, users_schema, level)
        with db:
            workload(db)
        digests.add(path.read_bytes())
    assert len(digests) == 1


def test_off_records_nothing_at_all(tmp_path: Path, users_schema: Schema):
    db, sink = build(tmp_path / "off.chendb", users_schema, TraceLevel.OFF)
    with db:
        workload(db)
    assert sink.stats.total_recorded == 0


def test_levels_are_strictly_nested_in_volume(tmp_path: Path, users_schema: Schema):
    totals: dict[TraceLevel, int] = {}
    for level in (TraceLevel.SUMMARY, TraceLevel.STORAGE, TraceLevel.VERBOSE):
        db, sink = build(tmp_path / f"vol-{level.name}.chendb", users_schema, level)
        with db:
            workload(db)
        totals[level] = sink.stats.total_recorded

    assert 0 < totals[TraceLevel.SUMMARY] < totals[TraceLevel.STORAGE]
    assert totals[TraceLevel.STORAGE] < totals[TraceLevel.VERBOSE]


def test_storage_level_emits_the_expected_event_families(
    tmp_path: Path, users_schema: Schema
):
    db, sink = build(tmp_path / "families.chendb", users_schema, TraceLevel.STORAGE)
    with db:
        workload(db)

    seen = {item.event_type for item in sink.snapshot()}
    assert {
        "DatabaseOpenedEvent",
        "PageAllocatedEvent",
        "PageReadEvent",
        "PageWriteEvent",
        "RecordInsertedEvent",
        "RecordDeletedEvent",
        "HeapScanEvent",
    } <= seen

    categories = {item.category for item in sink.snapshot()}
    assert categories == {
        EventCategory.LIFECYCLE,
        EventCategory.STORAGE,
        EventCategory.RECORD,
        # Creating a table and looking it up both go through the catalog now.
        EventCategory.CATALOG,
        # Every page read and write goes through the pool from Milestone 7.
        EventCategory.BUFFER_POOL,
    }


def test_page_read_events_report_the_real_file_offset(
    tmp_path: Path, users_schema: Schema
):
    db, sink = build(tmp_path / "offsets.chendb", users_schema, TraceLevel.STORAGE)
    with db:
        db.insert("users", (1, "x", None, True, 0.0))
        list(db.scan("users"))

    reads = [i.event for i in sink.snapshot() if i.event_type == "PageReadEvent"]
    assert reads
    for event in reads:
        assert event.file_offset == event.page_id * PAGE_SIZE
        # Constant "disk" until Milestone 7. The field was in the schema from
        # Milestone 1 for exactly this moment, so no consumer had to change.
        assert event.source in ("disk", "buffer_pool")
        assert event.duration_ns >= 0


def test_retention_is_bounded_and_losses_are_reported(
    tmp_path: Path, users_schema: Schema
):
    sink = RingBufferSink(capacity=50)
    db = Database.open(
        tmp_path / "bounded.chendb",
        page_size=PAGE_SIZE,
        tracer=Tracer(sink, TraceLevel.VERBOSE),
    )
    with db:
        db.create_table("users", users_schema)
        workload(db)

    stats = sink.stats
    assert stats.size == 50
    assert stats.dropped > 0
    assert stats.total_recorded == stats.size + stats.dropped


def test_tracing_overhead_is_bounded(tmp_path: Path, users_schema: Schema):
    """Tracing may cost time, but not an order of magnitude.

    Deliberately loose: this is a regression guard against something like an
    unguarded event construction in the page-read path, not a benchmark.
    ``benchmarks/trace_overhead.py`` produces the real numbers.
    """

    def timed(level: TraceLevel, name: str) -> float:
        db, _ = build(tmp_path / f"{name}.chendb", users_schema, level)
        with db:
            started = time.perf_counter()
            workload(db)
            return time.perf_counter() - started

    off = min(timed(TraceLevel.OFF, f"perf-off-{i}") for i in range(3))
    storage = min(timed(TraceLevel.STORAGE, f"perf-st-{i}") for i in range(3))

    assert storage < off * 6 + 0.05, (
        f"STORAGE tracing took {storage:.4f}s vs {off:.4f}s with tracing off"
    )
