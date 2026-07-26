"""Typed diagnostic events.

Every event is a frozen dataclass carrying only the facts the emitting
component knows.  Events deliberately have **no** ``to_json``, ``to_dict`` or
Pydantic base class: converting an event into an API payload is the job of
``engine/server/mappers.py``.  Keeping that conversion at the boundary is what
lets the engine run with zero third-party dependencies, and what stops
frontend concerns from leaking into storage code.

Events carry no sequence number or timestamp either.  The tracer stamps those
into a :class:`~engine.diagnostics.tracer.TraceRecord` envelope, so an event
object is pure data that a test can construct and compare with ``==``.

Milestone 1 defines the eleven event types below.  ``docs/event-schema.md``
specifies the full catalogue planned for Milestones 2-10; those classes are
added when the components that emit them are built, not before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from engine.diagnostics.levels import EventCategory, TraceLevel

__all__ = [
    "DatabaseClosedEvent",
    "DatabaseOpenedEvent",
    "DiagnosticEvent",
    "FileSyncEvent",
    "HeapScanEvent",
    "PageAllocatedEvent",
    "PageCompactedEvent",
    "PageFreedEvent",
    "PageReadEvent",
    "PageWriteEvent",
    "RecordDeletedEvent",
    "RecordInsertedEvent",
    "RecordReadEvent",
]

#: Where a page came from.  Milestone 1 always reads from ``"disk"``; the
#: buffer pool in Milestone 7 introduces ``"buffer_pool"``.
PageSource = Literal["disk", "buffer_pool"]


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """Base class for all diagnostic events.

    Subclasses override the two class variables; they are not instance fields,
    so they cost no memory per event and are available for filtering before an
    instance is ever built.
    """

    category: ClassVar[EventCategory] = EventCategory.LIFECYCLE
    level: ClassVar[TraceLevel] = TraceLevel.SUMMARY


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatabaseOpenedEvent(DiagnosticEvent):
    """A database file was opened or created."""

    category: ClassVar[EventCategory] = EventCategory.LIFECYCLE
    level: ClassVar[TraceLevel] = TraceLevel.SUMMARY

    database_id: str
    page_size: int
    page_count: int
    created: bool
    """True when the file did not exist and was initialised."""


@dataclass(frozen=True, slots=True)
class DatabaseClosedEvent(DiagnosticEvent):
    """A database file was closed cleanly."""

    category: ClassVar[EventCategory] = EventCategory.LIFECYCLE
    level: ClassVar[TraceLevel] = TraceLevel.SUMMARY

    database_id: str
    page_count: int
    pages_written: int
    """Cumulative writes over the life of the handle."""


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageAllocatedEvent(DiagnosticEvent):
    """A page was handed out by the allocator."""

    category: ClassVar[EventCategory] = EventCategory.STORAGE
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    page_id: int
    page_type: str
    recycled: bool
    """True if the page came off the free list instead of extending the file."""


@dataclass(frozen=True, slots=True)
class PageFreedEvent(DiagnosticEvent):
    """A page was returned to the free list."""

    category: ClassVar[EventCategory] = EventCategory.STORAGE
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    page_id: int
    previous_type: str


@dataclass(frozen=True, slots=True)
class PageReadEvent(DiagnosticEvent):
    """A page was fetched.

    ``duration_ns`` is measured with :func:`time.perf_counter_ns` around the
    read itself, so it includes the syscall but not the checksum verification.
    """

    category: ClassVar[EventCategory] = EventCategory.STORAGE
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    page_id: int
    file_offset: int
    source: PageSource
    duration_ns: int
    transaction_id: int | None = None
    """Always ``None`` until Milestone 8 introduces transactions."""


@dataclass(frozen=True, slots=True)
class PageWriteEvent(DiagnosticEvent):
    """A page was written back to the file (not necessarily durable yet)."""

    category: ClassVar[EventCategory] = EventCategory.STORAGE
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    page_id: int
    file_offset: int
    duration_ns: int
    transaction_id: int | None = None


@dataclass(frozen=True, slots=True)
class PageCompactedEvent(DiagnosticEvent):
    """Live records were slid together to reclaim tombstoned space."""

    category: ClassVar[EventCategory] = EventCategory.STORAGE
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    page_id: int
    reclaimed_bytes: int


@dataclass(frozen=True, slots=True)
class FileSyncEvent(DiagnosticEvent):
    """``fsync`` returned: everything written so far is durable."""

    category: ClassVar[EventCategory] = EventCategory.STORAGE
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    duration_ns: int
    pages_written_since_last_sync: int


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordInsertedEvent(DiagnosticEvent):
    """A tuple was placed in a heap page."""

    category: ClassVar[EventCategory] = EventCategory.RECORD
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    page_id: int
    slot_id: int
    length: int
    page_free_space_after: int


@dataclass(frozen=True, slots=True)
class RecordDeletedEvent(DiagnosticEvent):
    """A tuple's slot was tombstoned."""

    category: ClassVar[EventCategory] = EventCategory.RECORD
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    page_id: int
    slot_id: int


@dataclass(frozen=True, slots=True)
class RecordReadEvent(DiagnosticEvent):
    """A tuple was fetched by record id.

    Emitted at ``VERBOSE`` because a scan would otherwise produce one event per
    row, which is exactly the flood that trace levels exist to prevent.
    """

    category: ClassVar[EventCategory] = EventCategory.RECORD
    level: ClassVar[TraceLevel] = TraceLevel.VERBOSE

    page_id: int
    slot_id: int
    length: int


@dataclass(frozen=True, slots=True)
class HeapScanEvent(DiagnosticEvent):
    """A full heap scan started or finished."""

    category: ClassVar[EventCategory] = EventCategory.RECORD
    level: ClassVar[TraceLevel] = TraceLevel.SUMMARY

    action: Literal["started", "finished"]
    first_page_id: int
    pages_scanned: int = 0
    rows_emitted: int = 0
    duration_ns: int = 0


# --------------------------------------------------------------------------
# Parser (Milestone 2)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenizedEvent(DiagnosticEvent):
    """A SQL string was scanned into tokens."""

    category: ClassVar[EventCategory] = EventCategory.PARSER
    level: ClassVar[TraceLevel] = TraceLevel.OPERATOR

    source_length: int
    token_count: int
    duration_ns: int


@dataclass(frozen=True, slots=True)
class TokenEvent(DiagnosticEvent):
    """One token was produced.

    ``VERBOSE`` because a large script produces thousands, and the token stream
    is already available in full from the parse response.
    """

    category: ClassVar[EventCategory] = EventCategory.PARSER
    level: ClassVar[TraceLevel] = TraceLevel.VERBOSE

    index: int
    token_type: str
    lexeme: str
    start: int
    end: int
    keyword: str | None = None


@dataclass(frozen=True, slots=True)
class AstNodeCreatedEvent(DiagnosticEvent):
    """A parse rule completed and built a node.

    Emitted bottom-up, so the order shows how recursive descent assembles the
    tree: leaves before the nodes that contain them.
    """

    category: ClassVar[EventCategory] = EventCategory.PARSER
    level: ClassVar[TraceLevel] = TraceLevel.VERBOSE

    node_id: int
    node_type: str
    start: int
    end: int
    child_count: int


@dataclass(frozen=True, slots=True)
class ParsedEvent(DiagnosticEvent):
    """A script finished parsing."""

    category: ClassVar[EventCategory] = EventCategory.PARSER
    level: ClassVar[TraceLevel] = TraceLevel.OPERATOR

    statement_count: int
    node_count: int
    duration_ns: int


@dataclass(frozen=True, slots=True)
class ParseErrorEvent(DiagnosticEvent):
    """Parsing failed, with the exact position.

    ``OPERATOR`` rather than ``VERBOSE``: a failed parse is a headline event,
    and the editor needs the position to place a marker.
    """

    category: ClassVar[EventCategory] = EventCategory.PARSER
    level: ClassVar[TraceLevel] = TraceLevel.OPERATOR

    message: str
    start: int
    end: int
    line: int
    column: int
    expected: str = ""
    found: str = ""


# --------------------------------------------------------------------------
# Execution (Milestone 3)
# --------------------------------------------------------------------------

#: What an operator just did. ``next`` is a call *into* the operator;
#: ``row_emitted`` is a row coming *out* of it. Both are needed to show a row
#: travelling up the tree.
OperatorAction = Literal["opened", "next", "row_emitted", "exhausted", "closed"]


@dataclass(frozen=True, slots=True)
class OperatorEvent(DiagnosticEvent):
    """One step in the volcano iterator protocol."""

    category: ClassVar[EventCategory] = EventCategory.OPERATOR
    level: ClassVar[TraceLevel] = TraceLevel.OPERATOR

    operator_id: str
    operator_type: str
    action: OperatorAction
    input_rows: int
    output_rows: int
    row: str = ""
    """The row involved, rendered for display. Empty unless ``row_emitted``."""


@dataclass(frozen=True, slots=True)
class ExpressionEvalEvent(DiagnosticEvent):
    """One expression evaluated against one row.

    ``VERBOSE`` because a filter over a large table evaluates its predicate once
    per row, and the whole point of trace levels is to keep that off by default.
    """

    category: ClassVar[EventCategory] = EventCategory.OPERATOR
    level: ClassVar[TraceLevel] = TraceLevel.VERBOSE

    operator_id: str
    node_id: int
    expression: str
    result: str


@dataclass(frozen=True, slots=True)
class QueryExecutedEvent(DiagnosticEvent):
    """A statement finished."""

    category: ClassVar[EventCategory] = EventCategory.OPERATOR
    level: ClassVar[TraceLevel] = TraceLevel.SUMMARY

    statement_kind: str
    rows_returned: int
    rows_affected: int
    duration_ns: int
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionStateEvent(DiagnosticEvent):
    """A stepped execution changed state.

    Reported so the visualizer's controls reflect what the engine is actually
    doing rather than what the client last asked for.
    """

    category: ClassVar[EventCategory] = EventCategory.OPERATOR
    level: ClassVar[TraceLevel] = TraceLevel.SUMMARY

    execution_id: str
    state: str
    reason: str = ""
