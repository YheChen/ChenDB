"""Physical operators, in the volcano (iterator) model.

Every operator answers the same three questions — ``open()``, ``next()``,
``close()`` — and every operator's input is another operator.  A query is
therefore a tree, and running it means pulling one row at a time from the root:

    Project(email, age*2)          ← root; the caller pulls from here
        │  next()
        ▼
    Filter(age >= 18)
        │  next()
        ▼
    SeqScan(users)                 ← leaf; reads pages
        │
        ▼
      heap

`Project.next()` calls `Filter.next()`, which calls `SeqScan.next()` until it
finds a row its predicate accepts. Nothing is materialised: at any instant
exactly one row is in flight, so `LIMIT 1` over a billion-row table reads one
page. That is the whole reason the model is shaped this way, and it is why the
step debugger can pause *between* two `next()` calls and show you a single row
part-way up the tree.

Why not Python generators
-------------------------
A generator per operator would be shorter and is the idiomatic Python answer.
Explicit ``open``/``next``/``close`` is used instead because:

* it *is* the volcano interface, as described in Graefe's 1994 paper and as
  implemented in PostgreSQL (``ExecProcNode``) — the point here is to show the
  real thing;
* an operator can be asked for its statistics, its children and its state at any
  point, which a suspended generator frame cannot be;
* pausing between calls is trivial, whereas interrupting a generator mid-``yield``
  requires ``throw()`` and careful cleanup.

The cost is real: a Python method call per row per operator. PostgreSQL pays the
same cost in C and mitigates it with JIT expression compilation; DuckDB and
modern column stores abandon the model entirely for *vectorised* execution,
passing batches of ~2048 rows instead of one, which amortises the call overhead
over the batch. That is the single biggest performance idea this design gives up.

Complexity
----------
=================  ==============================================
Operator           Cost for *n* input rows
=================  ==============================================
``SeqScan``        O(pages) reads, O(n) rows
``Filter``         O(n) predicate evaluations, no extra I/O
``Project``        O(n * projections) evaluations
=================  ==============================================

All three are streaming and use O(1) memory. A blocking operator — sort, hash
aggregate, hash join — would need O(n), and none exist yet.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from engine.diagnostics.events import OperatorEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.executor.binder import ResultColumn
from engine.executor.controller import NULL_CONTROLLER, StepController, StepKind
from engine.executor.expression import describe_expression, evaluate, is_true
from engine.parser.ast import Expression
from engine.serialization.record import Row, decode_record
from engine.serialization.schema import Schema
from engine.storage.heap import HeapFile, RecordId

__all__ = [
    "ExecutionContext",
    "Filter",
    "Operator",
    "OperatorStats",
    "Project",
    "SeqScan",
    "describe_plan",
]


@dataclass(slots=True)
class ExecutionContext:
    """What every operator in a tree shares.

    Passing one object rather than threading a tracer and a controller through
    every constructor keeps operator signatures about the *query*, and means a
    later milestone can add a transaction or a buffer pool here without touching
    any operator.
    """

    tracer: Tracer = NULL_TRACER
    controller: StepController = NULL_CONTROLLER
    max_rows: int | None = None
    """A hard ceiling on rows returned, so an API request cannot be unbounded."""


@dataclass(slots=True)
class OperatorStats:
    """What one operator actually did, for the plan view's actual-vs-estimated."""

    next_calls: int = 0
    input_rows: int = 0
    output_rows: int = 0
    duration_ns: int = 0
    pages_read: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "next_calls": self.next_calls,
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "duration_ns": self.duration_ns,
            "pages_read": self.pages_read,
        }


class Operator(ABC):
    """One node in a physical plan.

    Lifecycle is strictly ``open()`` → ``next()``* → ``close()``. ``next()``
    returns ``None`` exactly once to mean "no more rows", and must keep
    returning ``None`` if called again.
    """

    __slots__ = ("_exhausted", "_opened", "context", "operator_id", "stats")

    def __init__(self, operator_id: str, context: ExecutionContext) -> None:
        self.operator_id = operator_id
        self.context = context
        self.stats = OperatorStats()
        self._opened = False
        self._exhausted = False

    # -- description -------------------------------------------------------

    @property
    def operator_type(self) -> str:
        return type(self).__name__

    @property
    @abstractmethod
    def children(self) -> tuple[Operator, ...]:
        """Input operators, left to right."""

    @property
    @abstractmethod
    def output_columns(self) -> tuple[ResultColumn, ...]:
        """The shape of the rows this operator emits."""

    @property
    def detail(self) -> str:
        """A one-line description: the predicate, the table, the projection."""
        return ""

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Acquire whatever the operator needs, and open its children."""
        if self._opened:
            return
        self._opened = True
        for child in self.children:
            child.open()
        self._on_open()
        self._emit("opened")
        self.context.controller.checkpoint(
            StepKind.OPERATOR_OPEN,
            operator_id=self.operator_id,
            detail=f"{self.operator_type} opened",
        )

    def next(self) -> Row | None:
        """Produce the next row, or ``None`` when there are no more."""
        if not self._opened:
            raise RuntimeError(f"{self.operator_id}: next() before open()")
        if self._exhausted:
            return None

        self.stats.next_calls += 1
        self._emit("next")
        self.context.controller.checkpoint(
            StepKind.OPERATOR_NEXT,
            operator_id=self.operator_id,
            detail=f"{self.operator_type}.next()",
        )

        started = time.perf_counter_ns()
        row = self._produce()
        self.stats.duration_ns += time.perf_counter_ns() - started

        if row is None:
            self._exhausted = True
            self._emit("exhausted")
            return None

        self.stats.output_rows += 1
        self._emit("row_emitted", row=row)
        self.context.controller.checkpoint(
            StepKind.ROW_EMITTED,
            operator_id=self.operator_id,
            detail=_render_row(row),
        )
        return row

    def close(self) -> None:
        """Release resources and close children. Safe to call more than once."""
        if not self._opened:
            return
        self._opened = False
        self._on_close()
        for child in self.children:
            child.close()
        self._emit("closed")

    def __iter__(self) -> Iterator[Row]:
        """Drain the operator. Convenience for tests and non-stepped execution."""
        self.open()
        try:
            while (row := self.next()) is not None:
                yield row
        finally:
            self.close()

    # -- hooks -------------------------------------------------------------

    @abstractmethod
    def _produce(self) -> Row | None:
        """Do the actual work of producing one row."""

    def _on_open(self) -> None:
        return None

    def _on_close(self) -> None:
        return None

    def _emit(self, action: str, row: Row | None = None) -> None:
        if not self.context.tracer.operator:
            return
        self.context.tracer.emit(
            OperatorEvent(
                operator_id=self.operator_id,
                operator_type=self.operator_type,
                action=action,  # type: ignore[arg-type]
                input_rows=self.stats.input_rows,
                output_rows=self.stats.output_rows,
                row=_render_row(row) if row is not None else "",
            )
        )

    def __repr__(self) -> str:
        return f"<{self.operator_type} {self.operator_id} {self.detail}>"


class SeqScan(Operator):
    """Reads every live row of a heap, in physical order.

    The only operator that touches storage. It decodes each record with the
    table's schema, so everything above it deals in Python values.

    Physical order is not insertion order after a delete — a tombstoned slot can
    be reused — which is exactly why ``SELECT`` without ``ORDER BY`` guarantees
    nothing about ordering.
    """

    __slots__ = ("_heap", "_last_record_id", "_rows", "_schema", "_table_name")

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        heap: HeapFile,
        schema: Schema,
        table_name: str,
    ) -> None:
        super().__init__(operator_id, context)
        self._heap = heap
        self._schema = schema
        self._table_name = table_name
        self._rows: Iterator[tuple[RecordId, bytes]] | None = None
        self._last_record_id: RecordId | None = None

    @property
    def children(self) -> tuple[Operator, ...]:
        return ()

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return tuple(
            ResultColumn(column.name, column.data_type) for column in self._schema
        )

    @property
    def detail(self) -> str:
        return f"table={self._table_name}"

    @property
    def last_record_id(self) -> RecordId | None:
        """Where the row most recently emitted came from. For the row inspector."""
        return self._last_record_id

    def _on_open(self) -> None:
        # A generator, so the first next() reads one page rather than the table.
        self._rows = self._heap.scan()

    def _on_close(self) -> None:
        self._rows = None
        self._last_record_id = None

    def _produce(self) -> Row | None:
        assert self._rows is not None
        try:
            record_id, payload = next(self._rows)
        except StopIteration:
            return None
        self.stats.input_rows += 1
        self._last_record_id = record_id
        return decode_record(self._schema, payload)


class Filter(Operator):
    """Passes through only rows whose predicate is exactly TRUE.

    NULL is not TRUE. A row whose predicate evaluates to unknown is dropped just
    like one that evaluates to false — which is why ``WHERE age > 18`` silently
    excludes rows with a NULL age. See
    :mod:`engine.executor.expression` for the truth tables.
    """

    __slots__ = ("_child", "_predicate", "_rows_rejected")

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        child: Operator,
        predicate: Expression,
    ) -> None:
        super().__init__(operator_id, context)
        self._child = child
        self._predicate = predicate
        self._rows_rejected = 0

    @property
    def children(self) -> tuple[Operator, ...]:
        return (self._child,)

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return self._child.output_columns

    @property
    def detail(self) -> str:
        return describe_expression(self._predicate)

    @property
    def rows_rejected(self) -> int:
        return self._rows_rejected

    def _produce(self) -> Row | None:
        # Loops until a row passes or the input runs out. One next() on a filter
        # can therefore cost many next() calls on its child — visible in the
        # step debugger, and the reason a selective filter over a big table is
        # slow without an index.
        while (row := self._child.next()) is not None:
            self.stats.input_rows += 1
            verdict = evaluate(
                self._predicate,
                row,
                tracer=self.context.tracer,
                operator_id=self.operator_id,
            )
            if is_true(verdict):
                return row
            self._rows_rejected += 1
        return None


class Project(Operator):
    """Evaluates the select list for each input row.

    Narrowing to fewer columns, computing an expression, and renaming are all the
    same operation. A projection that is exactly "every column in order" is
    dropped by the planner rather than executed — see
    :attr:`~engine.executor.binder.BoundSelect.is_identity_projection`.
    """

    __slots__ = ("_child", "_output_columns", "_projections")

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        child: Operator,
        projections: Sequence[Expression],
        output_columns: Sequence[ResultColumn],
    ) -> None:
        super().__init__(operator_id, context)
        self._child = child
        self._projections = tuple(projections)
        self._output_columns = tuple(output_columns)

    @property
    def children(self) -> tuple[Operator, ...]:
        return (self._child,)

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return self._output_columns

    @property
    def detail(self) -> str:
        return ", ".join(describe_expression(p) for p in self._projections)

    def _produce(self) -> Row | None:
        row = self._child.next()
        if row is None:
            return None
        self.stats.input_rows += 1
        return tuple(
            evaluate(
                projection,
                row,
                tracer=self.context.tracer,
                operator_id=self.operator_id,
            )
            for projection in self._projections
        )


# -- helpers ---------------------------------------------------------------


def _render_row(row: Sequence[Any] | None) -> str:
    """A compact row rendering for events and pause reasons."""
    if row is None:
        return ""
    parts = [
        "NULL" if value is None else (repr(value) if isinstance(value, str) else str(value))
        for value in row
    ]
    return f"({', '.join(parts)})"


def describe_plan(operator: Operator, indent: int = 0) -> str:
    """Render an operator tree as text, for the CLI and docs."""
    detail = f"  {operator.detail}" if operator.detail else ""
    lines = [f"{'  ' * indent}{'└─ ' if indent else ''}{operator.operator_type}{detail}"]
    for child in operator.children:
        lines.append(describe_plan(child, indent + 1))
    return "\n".join(lines)
