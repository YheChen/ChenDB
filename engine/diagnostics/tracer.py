"""The tracer: the single entry point components use to report what they do.

Design goal: **tracing must be nearly free when it is off.**  A storage engine
emits events in its hottest loops, so a check that costs a dictionary lookup or
an enum comparison would show up in benchmarks.

The pattern is a precomputed boolean per level::

    if tracer.storage:                      # one attribute load + branch
        tracer.emit(PageReadEvent(...))     # only now do we allocate

Building the event object is the real cost, and the guard skips it entirely.
Never write ``tracer.emit(...)`` unguarded in a hot path: Python evaluates the
argument before the call, so the event is constructed even when it is about to
be discarded.  ``benchmarks/trace_overhead.py`` measures both shapes.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Final

from engine.diagnostics.events import DiagnosticEvent
from engine.diagnostics.levels import EventCategory, TraceLevel
from engine.diagnostics.sink import DiagnosticSink, NullSink

__all__ = ["NULL_TRACER", "TraceRecord", "Tracer"]


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """An event plus the metadata the tracer stamps onto it.

    ``category``, ``level`` and ``event_type`` are copied out of the event's
    class so consumers can filter and render without importing every event
    class or calling ``type()``.
    """

    seq: int
    """Monotonic, gap-free within one tracer. Doubles as a pagination cursor."""

    timestamp_ns: int
    """``time.time_ns()`` at emission, wall clock, comparable across processes."""

    category: EventCategory
    level: TraceLevel
    event_type: str
    event: DiagnosticEvent


class Tracer:
    """Routes events from engine components to a sink, filtered by level."""

    __slots__ = (
        "_counter",
        "_level",
        "_sink",
        "operator",
        "storage",
        "summary",
        "verbose",
    )

    def __init__(
        self,
        sink: DiagnosticSink | None = None,
        level: TraceLevel = TraceLevel.OFF,
    ) -> None:
        self._sink: DiagnosticSink = sink if sink is not None else NullSink()
        self._counter = itertools.count(1)
        self._level = level
        self._refresh_flags()

    # -- configuration -----------------------------------------------------

    def _refresh_flags(self) -> None:
        """Cache one boolean per level so hot paths avoid enum comparisons."""
        level = self._level
        self.summary: bool = level >= TraceLevel.SUMMARY
        self.operator: bool = level >= TraceLevel.OPERATOR
        self.storage: bool = level >= TraceLevel.STORAGE
        self.verbose: bool = level >= TraceLevel.VERBOSE

    @property
    def level(self) -> TraceLevel:
        return self._level

    @level.setter
    def level(self, value: TraceLevel) -> None:
        self._level = TraceLevel(value)
        self._refresh_flags()

    @property
    def sink(self) -> DiagnosticSink:
        return self._sink

    @sink.setter
    def sink(self, value: DiagnosticSink) -> None:
        self._sink = value

    @property
    def enabled(self) -> bool:
        """True when any event at all would be recorded."""
        return self._level > TraceLevel.OFF

    def is_enabled(self, level: TraceLevel) -> bool:
        """Whether events at ``level`` are currently recorded.

        Prefer the cached attributes (:attr:`storage`, :attr:`verbose`, ...) in
        code that runs per page or per row.
        """
        return self._level >= level

    # -- emission ----------------------------------------------------------

    def emit(self, event: DiagnosticEvent) -> None:
        """Record ``event`` if its level passes the current threshold.

        Safe to call unguarded; the level check is repeated here so correctness
        never depends on the caller remembering the guard.  The guard is a
        performance optimisation, not a requirement.
        """
        event_class = type(event)
        if self._level < event_class.level:
            return
        self._sink.record(
            TraceRecord(
                seq=next(self._counter),
                timestamp_ns=time.time_ns(),
                category=event_class.category,
                level=event_class.level,
                event_type=event_class.__name__,
                event=event,
            )
        )


#: A tracer that discards everything, for components constructed without one.
#: Its flags are all ``False``, so guarded call sites compile down to a branch.
NULL_TRACER: Final[Tracer] = Tracer(NullSink(), TraceLevel.OFF)
