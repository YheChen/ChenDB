"""Where diagnostic events go.

A *sink* is anything that accepts :class:`~engine.diagnostics.tracer.TraceRecord`
objects.  The engine never knows which one is installed, so the same emitting
code serves a unit test, a CLI, a log file and the visualizer's WebSocket.

Retention is bounded by construction: :class:`RingBufferSink` holds at most
``capacity`` records and counts what it dropped.  An unbounded trace buffer is
a memory leak with extra steps. A scan of a large table at ``VERBOSE`` can
emit millions of events.
"""

from __future__ import annotations

import contextlib
import threading
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from engine.diagnostics.levels import EventCategory

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from engine.diagnostics.tracer import TraceRecord

__all__ = [
    "CallbackSink",
    "DiagnosticSink",
    "FanoutSink",
    "NullSink",
    "RingBufferSink",
    "SinkStats",
]


@runtime_checkable
class DiagnosticSink(Protocol):
    """Accepts trace records. Implementations must be safe to call from any thread."""

    def record(self, item: TraceRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class SinkStats:
    """A consistent point-in-time view of a sink's retention state."""

    capacity: int
    size: int
    total_recorded: int
    dropped: int

    @property
    def is_full(self) -> bool:
        return self.size >= self.capacity


class NullSink:
    """Discards everything.

    Installed whenever tracing is off, so emitting code never needs a ``None``
    check on the sink.
    """

    __slots__ = ()

    def record(self, item: TraceRecord) -> None:
        return None


class RingBufferSink:
    """Retains the most recent ``capacity`` records, oldest dropped first.

    All state changes happen under one lock, and :meth:`snapshot` copies under
    that same lock.  This is the "copy an immutable snapshot" strategy for
    diagnostics consistency: a reader can never observe a half-updated buffer,
    and the lock is released long before the data reaches a network socket.
    """

    __slots__ = ("_capacity", "_dropped", "_lock", "_records", "_total")

    def __init__(self, capacity: int = 10_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._records: deque[TraceRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._total = 0
        self._dropped = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, item: TraceRecord) -> None:
        with self._lock:
            if len(self._records) == self._capacity:
                self._dropped += 1
            self._records.append(item)
            self._total += 1

    def snapshot(
        self,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
        categories: Iterable[EventCategory] | None = None,
    ) -> tuple[TraceRecord, ...]:
        """Return matching records, oldest first.

        ``after_seq`` supports cursor pagination: a client remembers the last
        sequence number it saw and asks only for what came after it.  Because
        sequence numbers are assigned monotonically by the tracer, this is
        stable even while new events arrive.
        """
        with self._lock:
            items: Sequence[TraceRecord] = tuple(self._records)
        if after_seq is not None:
            items = [item for item in items if item.seq > after_seq]
        if categories is not None:
            wanted = frozenset(categories)
            items = [item for item in items if item.category in wanted]
        if limit is not None:
            items = items[:limit]
        return tuple(items)

    @property
    def stats(self) -> SinkStats:
        with self._lock:
            return SinkStats(
                capacity=self._capacity,
                size=len(self._records),
                total_recorded=self._total,
                dropped=self._dropped,
            )

    def clear(self) -> None:
        """Drop retained records. Counters keep running so drops stay visible."""
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class CallbackSink:
    """Forwards each record to a function.

    Used by the server to push events onto per-connection WebSocket queues.
    The callback must not block: it runs on whichever thread emitted the event,
    which is usually a thread in the middle of a storage operation.
    """

    __slots__ = ("_callback",)

    def __init__(self, callback: Callable[[TraceRecord], None]) -> None:
        self._callback = callback

    def record(self, item: TraceRecord) -> None:
        self._callback(item)


class FanoutSink:
    """Delivers every record to several sinks.

    A failing sink must not take down the engine, so exceptions are swallowed
    per sink.  Diagnostics are strictly best-effort: losing an event is
    acceptable, losing a query is not.
    """

    __slots__ = ("_sinks",)

    def __init__(self, *sinks: DiagnosticSink) -> None:
        self._sinks: list[DiagnosticSink] = list(sinks)

    def add(self, sink: DiagnosticSink) -> None:
        self._sinks.append(sink)

    def remove(self, sink: DiagnosticSink) -> None:
        with contextlib.suppress(ValueError):
            self._sinks.remove(sink)

    @property
    def sinks(self) -> tuple[DiagnosticSink, ...]:
        return tuple(self._sinks)

    def record(self, item: TraceRecord) -> None:
        for sink in tuple(self._sinks):
            try:
                sink.record(item)
            except Exception:
                continue
