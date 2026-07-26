"""Diagnostics: typed, optional, bounded observability for the engine.

Core components emit :class:`~engine.diagnostics.events.DiagnosticEvent`
objects through a :class:`~engine.diagnostics.tracer.Tracer` without knowing
who consumes them.  A CLI, a test, a log file and the visualizer's WebSocket
all attach the same way — by installing a sink.

    from engine.diagnostics import RingBufferSink, Tracer, TraceLevel

    sink = RingBufferSink(capacity=1000)
    tracer = Tracer(sink, TraceLevel.STORAGE)
    db = Database.open("demo.chendb", tracer=tracer)
    ...
    for item in sink.snapshot():
        print(item.seq, item.event_type, item.event)

Nothing in this package imports a web framework, and no event knows how to
serialize itself.  See ``docs/event-schema.md`` for the full catalogue.
"""

from engine.diagnostics.events import (
    DatabaseClosedEvent,
    DatabaseOpenedEvent,
    DiagnosticEvent,
    FileSyncEvent,
    HeapScanEvent,
    PageAllocatedEvent,
    PageCompactedEvent,
    PageFreedEvent,
    PageReadEvent,
    PageWriteEvent,
    RecordDeletedEvent,
    RecordInsertedEvent,
    RecordReadEvent,
)
from engine.diagnostics.levels import EventCategory, TraceLevel
from engine.diagnostics.sink import (
    CallbackSink,
    DiagnosticSink,
    FanoutSink,
    NullSink,
    RingBufferSink,
    SinkStats,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer, TraceRecord

__all__ = [
    # levels
    "TraceLevel",
    "EventCategory",
    # tracer
    "Tracer",
    "TraceRecord",
    "NULL_TRACER",
    # sinks
    "DiagnosticSink",
    "SinkStats",
    "NullSink",
    "RingBufferSink",
    "CallbackSink",
    "FanoutSink",
    # events
    "DiagnosticEvent",
    "DatabaseOpenedEvent",
    "DatabaseClosedEvent",
    "PageAllocatedEvent",
    "PageFreedEvent",
    "PageReadEvent",
    "PageWriteEvent",
    "PageCompactedEvent",
    "FileSyncEvent",
    "RecordInsertedEvent",
    "RecordDeletedEvent",
    "RecordReadEvent",
    "HeapScanEvent",
]
