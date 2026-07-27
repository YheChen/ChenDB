"""Diagnostics: trace levels, sinks, retention and event shapes."""

from __future__ import annotations

import dataclasses
import threading

import pytest

from engine.diagnostics import (
    CallbackSink,
    DatabaseOpenedEvent,
    EventCategory,
    FanoutSink,
    NullSink,
    PageReadEvent,
    RecordReadEvent,
    RingBufferSink,
    TraceLevel,
    Tracer,
    TraceRecord,
)
from engine.diagnostics.events import DiagnosticEvent


def make_page_read(page_id: int = 1) -> PageReadEvent:
    return PageReadEvent(
        page_id=page_id, file_offset=page_id * 4096, source="disk", duration_ns=1_000
    )


# -- levels ----------------------------------------------------------------


def test_off_records_nothing():
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.OFF)
    tracer.emit(make_page_read())
    tracer.emit(DatabaseOpenedEvent("db", 4096, 1, True))
    assert sink.snapshot() == ()
    assert tracer.enabled is False


def test_a_level_records_itself_and_everything_below_it():
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.STORAGE)

    tracer.emit(DatabaseOpenedEvent("db", 4096, 1, True))  # SUMMARY
    tracer.emit(make_page_read())  # STORAGE
    tracer.emit(RecordReadEvent(1, 0, 20))  # VERBOSE — filtered out

    assert [item.event_type for item in sink.snapshot()] == [
        "DatabaseOpenedEvent",
        "PageReadEvent",
    ]


def test_fast_path_flags_match_the_configured_level():
    tracer = Tracer(RingBufferSink(), TraceLevel.OFF)
    assert (tracer.summary, tracer.operator, tracer.storage, tracer.verbose) == (
        False,
        False,
        False,
        False,
    )

    tracer.level = TraceLevel.OPERATOR
    assert (tracer.summary, tracer.operator, tracer.storage, tracer.verbose) == (
        True,
        True,
        False,
        False,
    )

    tracer.level = TraceLevel.VERBOSE
    assert all((tracer.summary, tracer.operator, tracer.storage, tracer.verbose))


def test_level_can_be_changed_at_runtime():
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.OFF)
    tracer.emit(make_page_read())
    tracer.level = TraceLevel.STORAGE
    tracer.emit(make_page_read())
    assert len(sink.snapshot()) == 1


def test_is_enabled_agrees_with_emission():
    tracer = Tracer(RingBufferSink(), TraceLevel.OPERATOR)
    assert tracer.is_enabled(TraceLevel.SUMMARY)
    assert tracer.is_enabled(TraceLevel.OPERATOR)
    assert not tracer.is_enabled(TraceLevel.STORAGE)


# -- envelope --------------------------------------------------------------


def test_sequence_numbers_are_monotonic_and_gap_free():
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.VERBOSE)
    for page_id in range(10):
        tracer.emit(make_page_read(page_id))
    assert [item.seq for item in sink.snapshot()] == list(range(1, 11))


def test_the_envelope_carries_category_level_and_type():
    sink = RingBufferSink()
    Tracer(sink, TraceLevel.VERBOSE).emit(make_page_read())
    (item,) = sink.snapshot()
    assert item.category is EventCategory.STORAGE
    assert item.level is TraceLevel.STORAGE
    assert item.event_type == "PageReadEvent"
    assert item.timestamp_ns > 0


def test_events_are_frozen_value_objects():
    event = make_page_read()
    assert event == make_page_read()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.page_id = 99  # type: ignore[misc]


def test_every_event_declares_a_category_and_level():
    # Guards against a new event class forgetting its ClassVars and silently
    # inheriting LIFECYCLE/SUMMARY.
    for subclass in _all_subclasses(DiagnosticEvent):
        assert isinstance(subclass.category, EventCategory), subclass
        assert isinstance(subclass.level, TraceLevel), subclass


def _all_subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for subclass in cls.__subclasses__():
        found.append(subclass)
        found.extend(_all_subclasses(subclass))
    return found


# -- sinks -----------------------------------------------------------------


def test_null_sink_accepts_everything_and_keeps_nothing():
    sink = NullSink()
    sink.record(
        TraceRecord(1, 1, EventCategory.STORAGE, TraceLevel.STORAGE, "x", make_page_read())
    )


def test_ring_buffer_drops_oldest_and_counts_the_loss():
    sink = RingBufferSink(capacity=5)
    tracer = Tracer(sink, TraceLevel.VERBOSE)
    for page_id in range(12):
        tracer.emit(make_page_read(page_id))

    retained = sink.snapshot()
    assert len(retained) == 5
    assert [item.event.page_id for item in retained] == [7, 8, 9, 10, 11]

    stats = sink.stats
    assert stats.total_recorded == 12
    assert stats.dropped == 7
    assert stats.is_full


def test_capacity_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        RingBufferSink(capacity=0)


def test_snapshot_paginates_by_sequence_number():
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.VERBOSE)
    for page_id in range(10):
        tracer.emit(make_page_read(page_id))

    first = sink.snapshot(limit=4)
    assert [item.seq for item in first] == [1, 2, 3, 4]

    second = sink.snapshot(after_seq=first[-1].seq, limit=4)
    assert [item.seq for item in second] == [5, 6, 7, 8]


def test_snapshot_filters_by_category():
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.VERBOSE)
    tracer.emit(DatabaseOpenedEvent("db", 4096, 1, True))
    tracer.emit(make_page_read())
    tracer.emit(RecordReadEvent(1, 0, 20))

    storage_only = sink.snapshot(categories=[EventCategory.STORAGE])
    assert [item.event_type for item in storage_only] == ["PageReadEvent"]


def test_snapshot_is_an_immutable_copy():
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.VERBOSE)
    tracer.emit(make_page_read())

    snapshot = sink.snapshot()
    tracer.emit(make_page_read(2))

    # A consumer serialising `snapshot` cannot observe the later event, which
    # is what keeps a diagnostics response internally consistent.
    assert len(snapshot) == 1
    assert isinstance(snapshot, tuple)


def test_clear_keeps_counters_so_drops_stay_visible():
    sink = RingBufferSink(capacity=2)
    tracer = Tracer(sink, TraceLevel.VERBOSE)
    for page_id in range(5):
        tracer.emit(make_page_read(page_id))
    sink.clear()
    assert len(sink) == 0
    assert sink.stats.dropped == 3


def test_callback_sink_forwards_each_record():
    seen: list[TraceRecord] = []
    tracer = Tracer(CallbackSink(seen.append), TraceLevel.VERBOSE)
    tracer.emit(make_page_read())
    assert len(seen) == 1


def test_fanout_delivers_to_all_sinks():
    left, right = RingBufferSink(), RingBufferSink()
    tracer = Tracer(FanoutSink(left, right), TraceLevel.VERBOSE)
    tracer.emit(make_page_read())
    assert len(left.snapshot()) == len(right.snapshot()) == 1


def test_a_broken_sink_cannot_break_the_engine():
    class Exploding:
        def record(self, item: TraceRecord) -> None:
            raise RuntimeError("boom")

    healthy = RingBufferSink()
    tracer = Tracer(FanoutSink(Exploding(), healthy), TraceLevel.VERBOSE)
    tracer.emit(make_page_read())  # must not raise
    assert len(healthy.snapshot()) == 1


def test_ring_buffer_is_thread_safe():
    sink = RingBufferSink(capacity=10_000)
    tracer = Tracer(sink, TraceLevel.VERBOSE)
    events_per_thread = 500
    thread_count = 8

    def worker() -> None:
        for page_id in range(events_per_thread):
            tracer.emit(make_page_read(page_id))

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total = events_per_thread * thread_count
    assert sink.stats.total_recorded == total
    # Sequence numbers must be unique even under concurrency.
    assert len({item.seq for item in sink.snapshot()}) == total
