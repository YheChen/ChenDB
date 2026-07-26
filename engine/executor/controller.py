"""The execution controller: pausing a query mid-flight.

Step mode lets a client advance a query one operation at a time.  It is
implemented with real synchronisation, not sleeps: the query runs on its own
thread and *blocks* at checkpoints until the driver lets it continue.

    driver thread                    engine thread
    ─────────────                    ─────────────
    start()  ──────────────────────▶ execute()
                                       operator.next()
                                       checkpoint(OPERATOR_NEXT) ──┐
    ◀──── paused, reason reported ─────────────────────────────────┘ blocks
    step()   ──────────────────────▶ resumes
                                       ... emits a row
                                       checkpoint(ROW_EMITTED) ────┐
    ◀──── paused ─────────────────────────────────────────────────┘ blocks
    cancel() ──────────────────────▶ QueryCancelledError raised at
                                     the checkpoint, so operators unwind
                                     through their normal close() path

Two design points worth naming.

**Cancellation raises inside the engine thread**, at the next checkpoint, rather
than killing the thread. Operators then unwind through their own ``close()``,
releasing pages and file handles exactly as a successful query would. A thread
killed from outside would leak whatever it was holding — and Python cannot kill a
thread anyway.

**"Run until the next page read" is driven by the diagnostics bus**, not by a
hook in the pager. The controller registers itself as a sink; when a
:class:`PageReadEvent` arrives it pauses. The storage engine therefore knows
nothing about stepping, and every future "run until X" mode comes free the
moment X emits an event.

A paused query holds its database lock. That is inherent — it is suspended
mid-operation — so :meth:`cancel` deliberately does *not* need the lock: it sets
a flag and notifies, which is safe to call from any thread at any time.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from engine.diagnostics.events import (
    IndexSearchEvent,
    NodeSplitEvent,
    PageReadEvent,
    RangeScanEvent,
)
from engine.errors import QueryCancelledError

if TYPE_CHECKING:
    from engine.diagnostics.tracer import TraceRecord

__all__ = [
    "NULL_CONTROLLER",
    "ExecutionState",
    "PauseReason",
    "ResumeMode",
    "StepController",
    "StepKind",
]


class ExecutionState(StrEnum):
    """Where a stepped execution is."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL: Final = frozenset(
    {ExecutionState.FINISHED, ExecutionState.CANCELLED, ExecutionState.FAILED}
)


class StepKind(StrEnum):
    """The kinds of moment a query can be paused at."""

    OPERATOR_OPEN = "operator_open"
    OPERATOR_NEXT = "operator_next"
    ROW_EMITTED = "row_emitted"
    OPERATOR_CLOSE = "operator_close"
    PAGE_READ = "page_read"
    INDEX_OPERATION = "index_operation"
    """A B+ tree descent, search or split finished, or a row was fetched
    through an index."""


class ResumeMode(StrEnum):
    """How far to run before pausing again."""

    STEP = "step"
    """Pause at the very next checkpoint of any kind."""

    CONTINUE = "continue"
    """Run to completion without pausing."""

    UNTIL_ROW = "until_row"
    """Run until a row is emitted from the top of the tree."""

    UNTIL_PAGE_READ = "until_page_read"
    """Run until the storage engine reads a page."""

    UNTIL_OPERATOR = "until_operator"
    """Run until a specific operator is asked for its next row (step into)."""

    UNTIL_INDEX_OPERATION = "until_index_operation"
    """Run until the next B+ tree operation."""


#: Which checkpoint kinds each mode stops at. ``CONTINUE`` stops at none.
_STOPS_AT: Final[dict[ResumeMode, frozenset[StepKind]]] = {
    ResumeMode.STEP: frozenset(StepKind),
    ResumeMode.CONTINUE: frozenset(),
    ResumeMode.UNTIL_ROW: frozenset({StepKind.ROW_EMITTED}),
    ResumeMode.UNTIL_PAGE_READ: frozenset({StepKind.PAGE_READ}),
    ResumeMode.UNTIL_OPERATOR: frozenset({StepKind.OPERATOR_NEXT}),
    ResumeMode.UNTIL_INDEX_OPERATION: frozenset({StepKind.INDEX_OPERATION}),
}

#: Diagnostic events that become an ``INDEX_OPERATION`` checkpoint.  Adding a
#: "run until X" mode is this line plus one in ``_STOPS_AT`` — the payoff for
#: Milestone 1 building a general event bus rather than a logging call.
_INDEX_EVENTS: Final = (IndexSearchEvent, NodeSplitEvent, RangeScanEvent)


@dataclass(frozen=True, slots=True)
class PauseReason:
    """Why the query is currently paused."""

    kind: StepKind
    operator_id: str
    detail: str

    def __str__(self) -> str:
        where = f" in {self.operator_id}" if self.operator_id else ""
        return f"{self.kind.value}{where}: {self.detail}" if self.detail else (
            f"{self.kind.value}{where}"
        )


class StepController:
    """Coordinates a driver thread and one executing query.

    Not reusable: one controller per execution. :meth:`reset` on the API is
    implemented by discarding the execution and building a new one, because
    rewinding a half-consumed heap scan is not something operators support.
    """

    __slots__ = (
        "_cancel_requested",
        "_condition",
        "_mode",
        "_pause_reason",
        "_state",
        "_stepping",
        "_steps_taken",
        "_until_operator_id",
    )

    def __init__(self, *, stepping: bool = True) -> None:
        self._condition = threading.Condition()
        self._state = ExecutionState.PENDING
        # The first resume decides how far to run; until then, step mode pauses
        # at the first checkpoint and run mode never pauses at all.
        self._mode = ResumeMode.STEP if stepping else ResumeMode.CONTINUE
        self._stepping = stepping
        self._cancel_requested = False
        self._pause_reason: PauseReason | None = None
        self._until_operator_id: str | None = None
        self._steps_taken = 0

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> ExecutionState:
        with self._condition:
            return self._state

    @property
    def pause_reason(self) -> PauseReason | None:
        with self._condition:
            return self._pause_reason

    @property
    def steps_taken(self) -> int:
        with self._condition:
            return self._steps_taken

    @property
    def stepping(self) -> bool:
        return self._stepping

    @property
    def cancelled(self) -> bool:
        with self._condition:
            return self._cancel_requested

    # -- engine thread -----------------------------------------------------

    def mark_running(self) -> None:
        with self._condition:
            if self._state is ExecutionState.PENDING:
                self._state = ExecutionState.RUNNING
            self._condition.notify_all()

    def mark_finished(self, state: ExecutionState) -> None:
        """Called once, from the engine thread, when execution ends."""
        with self._condition:
            self._state = state
            self._pause_reason = None
            self._condition.notify_all()

    def checkpoint(
        self, kind: StepKind, *, operator_id: str = "", detail: str = ""
    ) -> None:
        """Pause here if the current mode says so. Called on the engine thread.

        Raises :exc:`QueryCancelledError` if cancellation was requested, so the
        operator stack unwinds through its normal ``close()`` path.
        """
        with self._condition:
            if self._cancel_requested:
                raise QueryCancelledError("execution cancelled")
            if not self._should_pause(kind, operator_id):
                return

            self._state = ExecutionState.PAUSED
            self._pause_reason = PauseReason(kind, operator_id, detail)
            self._steps_taken += 1
            self._condition.notify_all()

            # Wait for the driver. `wait()` releases the condition's lock, so
            # cancel() and resume() can both get in.
            while self._state is ExecutionState.PAUSED and not self._cancel_requested:
                self._condition.wait()

            if self._cancel_requested:
                raise QueryCancelledError("execution cancelled while paused")

    def _should_pause(self, kind: StepKind, operator_id: str) -> bool:
        """Caller must hold the condition."""
        if not self._stepping:
            return False
        if kind not in _STOPS_AT[self._mode]:
            return False
        if self._mode is ResumeMode.UNTIL_OPERATOR:
            return operator_id == self._until_operator_id
        return True

    # -- diagnostics sink --------------------------------------------------

    def record(self, item: TraceRecord) -> None:
        """Turn selected events into checkpoints, on the engine thread.

        Registered on the database's fanout sink, so this runs inside whichever
        component emitted the event. Keeping it here rather than hooking the
        pager and the B+ tree means neither of them has to know that stepping
        exists — and a future "run until X" mode costs one line here.
        """
        if not self._stepping:
            return
        event = item.event
        if isinstance(event, PageReadEvent):
            self.checkpoint(
                StepKind.PAGE_READ,
                detail=f"page {event.page_id} at offset {event.file_offset}",
            )
        elif isinstance(event, _INDEX_EVENTS):
            self.checkpoint(StepKind.INDEX_OPERATION, detail=_describe_index(event))

    # -- driver thread -----------------------------------------------------

    def resume(
        self, mode: ResumeMode = ResumeMode.STEP, *, operator_id: str | None = None
    ) -> None:
        """Let the query run again, up to whatever ``mode`` allows."""
        with self._condition:
            if self._state.is_terminal:
                return
            self._mode = mode
            self._until_operator_id = operator_id
            self._pause_reason = None
            self._state = ExecutionState.RUNNING
            self._condition.notify_all()

    def cancel(self) -> None:
        """Request cancellation. Safe from any thread, needs no engine lock."""
        with self._condition:
            if self._state.is_terminal:
                return
            self._cancel_requested = True
            # Wake a paused query so it can raise instead of waiting forever.
            self._condition.notify_all()

    def wait_for_pause_or_end(self, timeout: float) -> ExecutionState:
        """Block until the query pauses or finishes. Returns the state reached.

        A timeout is mandatory: without one, a bug in the engine thread would
        hang the HTTP worker that called this.
        """
        with self._condition:
            self._condition.wait_for(
                lambda: self._state is ExecutionState.PAUSED
                or self._state.is_terminal,
                timeout=timeout,
            )
            return self._state


class _NullController(StepController):
    """A controller that never pauses and cannot be cancelled.

    Used by non-stepped execution so operators can call ``checkpoint()``
    unconditionally. The override skips the lock entirely, which keeps normal
    execution free of synchronisation the way ``NULL_TRACER`` keeps it free of
    diagnostics.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(stepping=False)

    def checkpoint(
        self, kind: StepKind, *, operator_id: str = "", detail: str = ""
    ) -> None:
        return None

    def record(self, item: TraceRecord) -> None:
        return None


#: Shared no-op controller for queries that are not being stepped.
NULL_CONTROLLER: Final[StepController] = _NullController()


def _describe_index(event: IndexSearchEvent | NodeSplitEvent | RangeScanEvent) -> str:
    """A one-line pause reason for a B+ tree event."""
    match event:
        case IndexSearchEvent():
            return (
                f"{event.index_name}: search {event.key} → "
                f"{event.matches} match(es) in {event.pages_visited} page(s)"
            )
        case NodeSplitEvent():
            kind = "root split" if event.is_root_split else "split"
            return (
                f"{event.index_name}: {kind} page {event.page_id} → "
                f"{event.new_page_id}, promoted {event.promoted_key}"
            )
        case RangeScanEvent():
            low = event.low or "-∞"
            high = event.high or "+∞"
            return (
                f"{event.index_name}: range [{low}, {high}] → "
                f"{event.rows_emitted} row(s) over {event.leaves_visited} leaf/leaves"
            )
