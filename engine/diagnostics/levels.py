"""Trace levels and event categories.

A trace level is a *verbosity threshold*: an event is recorded when its own
level is less than or equal to the tracer's configured level.  Levels are
spaced by ten so new tiers can be slotted in without renumbering anything that
has already been persisted in a saved trace.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

__all__ = ["EventCategory", "TraceLevel"]


class TraceLevel(IntEnum):
    """How much internal detail the engine reports.

    Higher levels are strictly supersets of lower ones.  The cost of each level
    is measured in ``benchmarks/trace_overhead.py``.
    """

    OFF = 0
    """No events. The tracer's fast-path flags are all false."""

    SUMMARY = 10
    """One event per top-level operation: database opened, query finished."""

    OPERATOR = 20
    """Adds query-plan operator lifecycle events. Used from Milestone 3."""

    STORAGE = 30
    """Adds page reads/writes, allocations and record I/O. The default for the
    visualizer: enough to animate the storage engine without flooding."""

    VERBOSE = 40
    """Everything, including per-expression evaluation and checksum checks.
    Expect a large constant-factor slowdown."""


class EventCategory(StrEnum):
    """Which subsystem produced an event.

    Consumers filter on this, so the full set is declared up front even though
    Milestone 1 only emits the first three.  A string enum keeps the value
    self-describing when it reaches JSON.
    """

    LIFECYCLE = "lifecycle"
    STORAGE = "storage"
    RECORD = "record"

    # Reserved for later milestones. No events use these yet.
    PARSER = "parser"  # M2: tokens, AST nodes
    OPERATOR = "operator"  # M3: volcano iterator calls
    CATALOG = "catalog"  # M4: schema lookups
    INDEX = "index"  # M5: B+ tree descent, splits, merges
    PLANNER = "planner"  # M6: logical/physical plans, cost estimates
    BUFFER_POOL = "buffer_pool"  # M7: hits, misses, pins, evictions
    TRANSACTION = "transaction"  # M8: begin/commit/rollback
    WAL = "wal"  # M9: appends, flushes, checkpoints
    RECOVERY = "recovery"  # M9: analysis, redo, undo
    LOCK = "lock"  # M10: acquire, wait, deadlock
    MVCC = "mvcc"  # M10: version visibility decisions
