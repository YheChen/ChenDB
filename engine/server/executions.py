"""Stepped executions: a registry of queries paused mid-flight.

A normal query runs inside one HTTP request.  A *stepped* query outlives its
request: the client starts it, then drives it with a series of follow-up calls.
Something has to own the thread in between, and that is :class:`ExecutionStore`.

    POST /query/step        →  start a thread, run to the first checkpoint, return
    POST /executions/{id}/next   →  resume, run to the next checkpoint, return
    POST /executions/{id}/cancel  →  set the flag; the thread unwinds itself
    GET  /executions/{id}   →  current state without changing anything

Three properties this has to get right, all of them failure modes rather than
features:

**Bounded.** At most :attr:`ServerConfig.max_executions` are retained; starting
one more evicts the oldest finished execution. A registry that grows forever is a
leak with a thread attached to each entry.

**No orphaned threads.** Every execution holds the database lock while it runs.
A client that starts a stepped query and vanishes would hold that lock forever,
so each execution carries a deadline: :meth:`ExecutionStore.reap` cancels any
paused execution that has not been touched within
:attr:`ServerConfig.execution_idle_timeout_seconds`, and it is called on every
store operation. There is no background timer to leak instead.

**Cancellation never needs the lock.** :meth:`StepController.cancel` only sets a
flag and notifies a condition, so cancelling a query that is *holding* the
database lock works, which is the only way to get the lock back.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engine.diagnostics.events import ExecutionStateEvent
from engine.errors import ChenDBError
from engine.executor.controller import (
    ExecutionState,
    PauseReason,
    ResumeMode,
    StepController,
)
from engine.executor.engine import QueryResult, execute_statement
from engine.parser.analyze import analyze_sql
from engine.parser.ast import Statement

if TYPE_CHECKING:
    from engine.server.config import ServerConfig
    from engine.server.workspace import ManagedDatabase

__all__ = ["Execution", "ExecutionNotFound", "ExecutionStore"]


class ExecutionNotFound(ChenDBError):
    """No execution with that id, or it was evicted."""


@dataclass(slots=True)
class Execution:
    """One stepped query and the thread running it."""

    execution_id: str
    database_id: str
    sql: str
    statement_kind: str
    controller: StepController
    created_ns: int
    last_touched_ns: int
    thread: threading.Thread | None = None
    result: QueryResult | None = None
    error: str = ""
    partial_rows: list[tuple[object, ...]] = field(default_factory=list)
    """Rows emitted so far. Populated as the query runs, so a paused execution
    can show its output up to this point rather than nothing."""

    @property
    def state(self) -> ExecutionState:
        return self.controller.state

    @property
    def pause_reason(self) -> PauseReason | None:
        return self.controller.pause_reason

    @property
    def age_seconds(self) -> float:
        return (time.monotonic_ns() - self.created_ns) / 1e9

    @property
    def idle_seconds(self) -> float:
        return (time.monotonic_ns() - self.last_touched_ns) / 1e9

    def touch(self) -> None:
        self.last_touched_ns = time.monotonic_ns()


class ExecutionStore:
    """Owns every in-flight stepped execution for the whole server."""

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._executions: dict[str, Execution] = {}
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    # -- lifecycle ---------------------------------------------------------

    def start(self, managed: ManagedDatabase, sql: str) -> Execution:
        """Parse ``sql``, start it on a thread, and run to the first checkpoint.

        Only a single statement may be stepped: stepping a script would need a
        notion of "which statement am I in" that the controller does not model,
        and silently stepping only the first would be worse than refusing.
        """
        self.reap()

        outcome = analyze_sql(sql, tracer=managed.tracer)
        if outcome.error is not None:
            raise outcome.error
        if len(outcome.statements) != 1:
            raise ChenDBError(
                f"step mode runs exactly one statement, got {len(outcome.statements)}"
            )
        statement = outcome.statements[0]

        execution = Execution(
            execution_id=f"exec_{next(self._counter)}_{uuid.uuid4().hex[:8]}",
            database_id=managed.database_id,
            sql=sql,
            statement_kind=statement.node_type,
            controller=StepController(stepping=True),
            created_ns=time.monotonic_ns(),
            last_touched_ns=time.monotonic_ns(),
        )

        with self._lock:
            self._evict_if_needed()
            self._executions[execution.execution_id] = execution

        # The controller is registered as a diagnostics sink so "run until the
        # next page read" works without the pager knowing about stepping.
        managed.subscribe(execution.controller)

        execution.thread = threading.Thread(
            target=self._run,
            args=(execution, managed, statement),
            name=f"chendb-{execution.execution_id}",
            daemon=True,
        )
        execution.thread.start()

        # Block briefly for the first checkpoint so the client's very first
        # response already shows a paused query rather than "pending".
        execution.controller.wait_for_pause_or_end(
            timeout=self._config.execution_step_timeout_seconds
        )
        return execution

    def _run(
        self,
        execution: Execution,
        managed: ManagedDatabase,
        statement: Statement,
    ) -> None:
        """The engine thread. Holds the database lock for the whole execution."""
        try:
            with managed.use() as db:
                execution.result = execute_statement(
                    statement,
                    db,
                    tracer=managed.tracer,
                    controller=execution.controller,
                    max_rows=self._config.max_rows_per_query,
                )
            state = (
                ExecutionState.CANCELLED
                if execution.result and execution.result.cancelled
                else ExecutionState.FINISHED
            )
        except ChenDBError as exc:
            execution.error = str(exc)
            state = ExecutionState.FAILED
        except Exception as exc:
            execution.error = f"{type(exc).__name__}: {exc}"
            state = ExecutionState.FAILED
        finally:
            managed.unsubscribe(execution.controller)

        execution.controller.mark_finished(state)
        if managed.tracer.summary:
            managed.tracer.emit(
                ExecutionStateEvent(
                    execution_id=execution.execution_id,
                    state=state.value,
                    reason=execution.error,
                )
            )

    # -- driving -----------------------------------------------------------

    def get(self, execution_id: str) -> Execution:
        self.reap()
        with self._lock:
            execution = self._executions.get(execution_id)
        if execution is None:
            raise ExecutionNotFound(f"no execution {execution_id!r}")
        execution.touch()
        return execution

    def resume(
        self,
        execution_id: str,
        mode: ResumeMode,
        *,
        operator_id: str | None = None,
    ) -> Execution:
        """Let an execution run, then wait for it to pause or finish."""
        execution = self.get(execution_id)
        if execution.state.is_terminal:
            return execution

        execution.controller.resume(mode, operator_id=operator_id)
        execution.controller.wait_for_pause_or_end(
            timeout=self._config.execution_step_timeout_seconds
        )
        execution.touch()
        return execution

    def cancel(self, execution_id: str) -> Execution:
        """Cancel and wait for the thread to unwind."""
        execution = self.get(execution_id)
        execution.controller.cancel()
        # The controller was notified, so a paused thread wakes and raises. Join
        # so the caller knows the database lock has actually been released.
        if execution.thread is not None:
            execution.thread.join(timeout=self._config.execution_step_timeout_seconds)
        execution.touch()
        return execution

    def list(self, database_id: str | None = None) -> list[Execution]:
        self.reap()
        with self._lock:
            executions = list(self._executions.values())
        if database_id is not None:
            executions = [e for e in executions if e.database_id == database_id]
        return sorted(executions, key=lambda e: e.created_ns, reverse=True)

    # -- housekeeping ------------------------------------------------------

    def reap(self) -> int:
        """Cancel executions whose client has gone away. Returns how many.

        Called on every store operation rather than from a timer: a background
        thread would be one more thing to shut down, and there is no need to reap
        when nobody is using the API.
        """
        timeout = self._config.execution_idle_timeout_seconds
        with self._lock:
            stale = [
                execution
                for execution in self._executions.values()
                if not execution.state.is_terminal and execution.idle_seconds > timeout
            ]
        for execution in stale:
            execution.controller.cancel()
            if execution.thread is not None:
                execution.thread.join(timeout=1.0)
        return len(stale)

    def _evict_if_needed(self) -> None:
        """Drop the oldest finished executions. Caller holds the lock."""
        while len(self._executions) >= self._config.max_executions:
            finished = [
                execution_id
                for execution_id, execution in self._executions.items()
                if execution.state.is_terminal
            ]
            if finished:
                # dict preserves insertion order, so the first finished entry is
                # the oldest one.
                self._executions.pop(finished[0])
                continue
            # Everything is still live. Cancel the oldest rather than refusing
            # the new request: a stuck execution must not block the server.
            oldest_id = next(iter(self._executions))
            oldest = self._executions.pop(oldest_id)
            oldest.controller.cancel()

    def shutdown(self) -> None:
        """Cancel everything and wait. Called from the app's lifespan teardown."""
        with self._lock:
            executions = list(self._executions.values())
            self._executions.clear()
        for execution in executions:
            execution.controller.cancel()
            if execution.thread is not None:
                execution.thread.join(timeout=2.0)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._executions)
