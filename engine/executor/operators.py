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
=================  ==================================================
Operator           Cost for *n* input rows
=================  ==================================================
``SeqScan``        O(pages) reads, O(n) rows
``IndexScan``      O(log n) descent + one heap read per matching row
``Filter``         O(n) predicate evaluations, no extra I/O
``Project``        O(n * projections) evaluations
=================  ==================================================

All four are streaming and use O(1) memory. A blocking operator — sort, hash
aggregate, hash join — would need O(n), and none exist yet.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from engine.concurrency.snapshot import Snapshot, visible
from engine.diagnostics.events import OperatorEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import RecordNotFoundError
from engine.executor.binder import BoundAggregate, BoundSortKey, ResultColumn, RowLayout
from engine.executor.controller import NULL_CONTROLLER, StepController, StepKind
from engine.executor.expression import (
    check_numeric_range,
    describe_expression,
    evaluate,
    is_true,
)
from engine.index.bplustree import BPlusTree
from engine.index.key import SMALLEST_VALUE_KEY, describe_key
from engine.parser.ast import AggregateFunction, Expression
from engine.serialization.record import (
    Row,
    decode_record,
    read_tuple_header,
    strip_tuple_header,
)
from engine.serialization.schema import Schema
from engine.storage.heap import HeapFile, RecordId

__all__ = [
    "ExecutionContext",
    "Filter",
    "HashAggregate",
    "HashJoin",
    "IndexScan",
    "JoinOperator",
    "Limit",
    "NestedLoopJoin",
    "Operator",
    "OperatorStats",
    "Project",
    "ScanOperator",
    "SeqScan",
    "Sort",
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
    planner_options: Any = None
    """A :class:`~engine.planner.physical.PlannerOptions`, or ``None`` for the
    defaults. Typed loosely to keep the operator layer from importing the
    planner, which would make the dependency run the wrong way."""
    snapshot: Snapshot | None = None
    """The view this query reads through, from Milestone 10.

    ``None`` means "every version" and is what the vacuum and the page
    inspector want — not a default anybody executing a query should get, which
    is why :func:`~engine.executor.engine.execute_statement` always supplies
    one.

    This field is the reason the docstring above says a later milestone could
    add a transaction here without touching any operator. It was not quite
    true: the two scans had to learn to skip a version, because filtering
    invisible rows in a ``Filter`` above them would mean decoding every dead
    row first.
    """


@dataclass(slots=True)
class OperatorStats:
    """What one operator actually did, for the plan view's actual-vs-estimated."""

    next_calls: int = 0
    input_rows: int = 0
    output_rows: int = 0
    rows_skipped: int = 0
    """Versions a scan walked past because this snapshot could not see them.
    Non-zero means dead weight the vacuum has not reclaimed, and it is the
    reader-side cost of never blocking a writer."""
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


class ScanOperator(Operator):
    """A leaf that reads rows of one table from storage.

    Two of them exist — :class:`SeqScan` and :class:`IndexScan` — and they are
    interchangeable: same output columns, same row shape, same ``last_record_id``
    for the row inspector.  That interchangeability *is* the access path
    abstraction, and it is what lets the planner swap one for the other without
    anything above the leaf noticing.
    """

    __slots__ = ("_heap", "_last_record_id", "_layout", "_schema", "_table_name")

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        heap: HeapFile,
        schema: Schema,
        table_name: str,
        layout: RowLayout | None = None,
    ) -> None:
        super().__init__(operator_id, context)
        self._heap = heap
        self._schema = schema
        self._table_name = table_name
        self._layout = layout
        self._last_record_id: RecordId | None = None

    @property
    def children(self) -> tuple[Operator, ...]:
        return ()

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return tuple(ResultColumn(column.name, column.data_type) for column in self._schema)

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def last_record_id(self) -> RecordId | None:
        """Where the row most recently emitted came from. For the row inspector."""
        return self._last_record_id

    def _visible_row(self, payload: bytes) -> Row | None:
        """Decode a version, or ``None`` if this snapshot cannot see it.

        The header check comes **before** the decode, and that ordering is the
        only reason MVCC's read cost is bearable: an invisible version costs
        eight bytes of unpacking rather than a walk of every column.

        Shared by both scans, because "which versions can I see" is a property
        of the reader and not of how it found the row.
        """
        header = read_tuple_header(payload)
        snapshot = self.context.snapshot
        if snapshot is not None and not visible(header, snapshot):
            self.stats.rows_skipped += 1
            return None
        row = decode_record(self._schema, strip_tuple_header(payload))
        return self._place(row)

    def _place(self, row: Row) -> Row:
        """Put this table's columns where the joined row expects them.

        Below a join every row is the full width of the ``FROM``, with the
        tables not yet joined left empty. That is what lets a bound column
        index — computed once, by the binder, against the *written* order —
        stay correct however the planner decides to reorder the joins.

        With one table the layout is the identity and this is a no-op, which is
        why nothing before Milestone 13 pays for it.
        """
        layout = self._layout
        if layout is None or layout.total == layout.width:
            return row
        wide = layout.blank()
        wide[layout.offset : layout.offset + layout.width] = row
        return tuple(wide)

    def _on_close(self) -> None:
        self._last_record_id = None


class SeqScan(ScanOperator):
    """Reads every live row of a heap, in physical order.

    Physical order is not insertion order after a delete — a tombstoned slot can
    be reused — which is exactly why ``SELECT`` without ``ORDER BY`` guarantees
    nothing about ordering.
    """

    __slots__ = ("_rows",)

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        heap: HeapFile,
        schema: Schema,
        table_name: str,
        layout: RowLayout | None = None,
    ) -> None:
        super().__init__(
            operator_id,
            context,
            heap=heap,
            schema=schema,
            table_name=table_name,
            layout=layout,
        )
        self._rows: Iterator[tuple[RecordId, bytes]] | None = None

    @property
    def detail(self) -> str:
        return f"table={self._table_name}"

    def _on_open(self) -> None:
        # A generator, so the first next() reads one page rather than the table.
        self._rows = self._heap.scan()

    def _on_close(self) -> None:
        super()._on_close()
        self._rows = None

    def _produce(self) -> Row | None:
        assert self._rows is not None
        while True:
            try:
                record_id, payload = next(self._rows)
            except StopIteration:
                return None
            self.stats.input_rows += 1
            row = self._visible_row(payload)
            if row is None:
                # A version this snapshot cannot see. Counted as input and not
                # as output, so the plan view shows the reader paying for dead
                # weight — which is what an overdue vacuum looks like.
                continue
            self._last_record_id = record_id
            return row


class IndexScan(ScanOperator):
    """Reads rows through a B+ tree instead of walking the heap.

    Two costs, and only the first one is the win::

        tree.range_scan(low, high)  →  (key, record_id)   O(log n) + matches
        heap.get(record_id)         →  the row            one page read *each*

    The descent is cheap.  The fetches are not: record ids come out in *key*
    order, which is unrelated to where the rows physically sit, so each one is a
    random page read.  A query matching most of a table therefore does more I/O
    through the index than a sequential scan would — the crossover is somewhere
    around a few percent selectivity on real hardware, and it is exactly what a
    cost model exists to estimate.  Milestone 5 chooses by rule and can get this
    wrong; Milestone 6 is where it starts choosing by cost.

    Rows come out in index order, which is a genuine bonus: an ``ORDER BY`` on
    the indexed column needs no sort. Nothing exploits that yet.
    """

    __slots__ = (
        "_entries",
        "_high",
        "_include_high",
        "_include_low",
        "_index_name",
        "_low",
        "_rows_fetched",
        "_tree",
    )

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        heap: HeapFile,
        schema: Schema,
        table_name: str,
        tree: BPlusTree,
        low: bytes | None,
        high: bytes | None,
        include_low: bool = True,
        include_high: bool = True,
        layout: RowLayout | None = None,
    ) -> None:
        super().__init__(
            operator_id,
            context,
            heap=heap,
            schema=schema,
            table_name=table_name,
            layout=layout,
        )
        self._tree = tree
        self._index_name = tree.name
        self._low = low
        self._high = high
        self._include_low = include_low
        self._include_high = include_high
        self._entries: Iterator[tuple[bytes, RecordId]] | None = None
        self._rows_fetched = 0

    @property
    def detail(self) -> str:
        return f"index={self._index_name} {self.condition}"

    @property
    def condition(self) -> str:
        """The index condition, rendered the way ``EXPLAIN`` would show it."""
        data_type = self._tree.data_type
        if (
            self._low is not None
            and self._low == self._high
            and self._include_low
            and self._include_high
        ):
            return f"key = {describe_key(self._low, data_type)}"

        parts: list[str] = []
        if self._low == SMALLEST_VALUE_KEY and self._include_low:
            # Not a value the user wrote: it is the sentinel that keeps NULL keys
            # out of a range bounded only from above. Say what it means.
            parts.append("key IS NOT NULL")
        elif self._low is not None:
            operator = ">=" if self._include_low else ">"
            parts.append(f"key {operator} {describe_key(self._low, data_type)}")
        if self._high is not None:
            operator = "<=" if self._include_high else "<"
            parts.append(f"key {operator} {describe_key(self._high, data_type)}")
        return " AND ".join(parts) if parts else "full index scan"

    @property
    def rows_fetched(self) -> int:
        """Heap fetches performed — one per matching index entry."""
        return self._rows_fetched

    def _on_open(self) -> None:
        self._entries = self._tree.range_scan(
            self._low,
            self._high,
            include_low=self._include_low,
            include_high=self._include_high,
        )

    def _on_close(self) -> None:
        super()._on_close()
        # Closing the generator runs its finally blocks, so an abandoned scan
        # does not leave the index's range-scan event unemitted.
        if self._entries is not None:
            self._entries.close()
        self._entries = None

    def _produce(self) -> Row | None:
        assert self._entries is not None
        while True:
            try:
                _, record_id = next(self._entries)
            except StopIteration:
                return None
            self.stats.input_rows += 1
            self.context.controller.checkpoint(
                StepKind.INDEX_OPERATION,
                operator_id=self.operator_id,
                detail=f"{self._index_name}: fetch {record_id}",
            )
            try:
                payload = self._heap.get(record_id)
            except RecordNotFoundError:
                # The index points at a tombstoned row. Cannot happen while
                # Database.delete maintains both, but an index rebuilt from a
                # crashed write could disagree — skipping is the same recovery
                # PostgreSQL performs when it finds a dead tuple through an index.
                continue
            self._rows_fetched += 1
            row = self._visible_row(payload)
            if row is None:
                continue
            self._last_record_id = record_id
            return row


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
            if is_true(verdict, clause="WHERE"):
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


class JoinOperator(Operator):
    """Two inputs, one output row per matching pair — and the unmatched ones.

    Both algorithms below place the right side's columns into the left side's
    row by *slice*, at the offsets :class:`RowLayout` fixed. Merging by "take
    whichever side is not None" would be shorter and wrong: a genuine SQL NULL
    is indistinguishable from an empty slot, so a NULL on the left would be
    silently overwritten by whatever the right side happened to have there.

    **NULL extension is free here, and that is a consequence of the row layout
    rather than a coincidence.** Every row below the topmost join is already the
    full width of the ``FROM``, with the tables not yet joined left as ``None``.
    So a left row that found no partner *is* its own NULL-extended form — the
    right side's slots have never been written — and the same holds mirrored for
    an unmatched right row, whose subplan never touched the left side's slots.
    Milestone 13 paid for that layout in row width; this is the refund.

    ``preserve_left`` and ``preserve_right`` describe the *physical* inputs, not
    the ``LEFT`` or ``RIGHT`` in the query. See
    :class:`~engine.planner.physical.PhysicalJoin`.
    """

    __slots__ = (
        "_left",
        "_predicate",
        "_preserve_left",
        "_preserve_right",
        "_right",
        "_right_slices",
    )

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        left: Operator,
        right: Operator,
        predicate: Expression,
        right_slices: tuple[tuple[int, int], ...],
        preserve_left: bool = False,
        preserve_right: bool = False,
    ) -> None:
        super().__init__(operator_id, context)
        self._left = left
        self._right = right
        self._predicate = predicate
        self._right_slices = right_slices
        self._preserve_left = preserve_left
        self._preserve_right = preserve_right

    @property
    def detail(self) -> str:
        label = (
            "FULL"
            if self._preserve_left and self._preserve_right
            else "LEFT"
            if self._preserve_left
            else "RIGHT"
            if self._preserve_right
            else ""
        )
        rendered = describe_expression(self._predicate)
        return f"{label} {rendered}" if label else rendered

    @property
    def children(self) -> tuple[Operator, ...]:
        return (self._left, self._right)

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return self._left.output_columns + self._right.output_columns

    def _merge(self, left: Row, right: Row) -> Row:
        merged = list(left)
        for offset, width in self._right_slices:
            merged[offset : offset + width] = right[offset : offset + width]
        return tuple(merged)

    def _matches(self, row: Row) -> bool:
        return is_true(
            evaluate(
                self._predicate,
                row,
                tracer=self.context.tracer,
                operator_id=self.operator_id,
            ),
            clause="a join condition",
        )


class NestedLoopJoin(JoinOperator):
    """For every left row, every right row. The algorithm that always works.

    The right side is drained into memory once on ``open`` rather than re-read
    per left row. That is not a small detail: re-opening the child would turn
    ``O(n·m)`` comparisons into ``O(n·m)`` *scans*, and it is the difference
    between slow and unusable. It also means the memory cost is the right side,
    which is why the planner puts the smaller estimate there.

    Even so it loses to a hash join on every equijoin the cost model has ever
    been shown, and that is the point of keeping it: a planner with one
    algorithm has nothing to be right about.
    """

    __slots__ = ("_buffered", "_left_row", "_position")

    __slots__ = (
        "_buffered",
        "_draining",
        "_left_matched",
        "_left_row",
        "_position",
        "_right_matched",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._buffered: list[Row] = []
        self._left_row: Row | None = None
        self._position = 0
        self._left_matched = False
        self._draining = False
        self._right_matched: set[int] = set()
        """Indices into ``_buffered``. Indices, not rows: two identical right rows
        are two rows, and a set of row *values* would treat them as one — so an
        unmatched duplicate would go missing from a RIGHT join."""

    def _on_open(self) -> None:
        self._buffered = list(iter(self._right))
        self._left_row = None
        self._position = 0
        self._left_matched = False
        self._draining = False
        self._right_matched = set()

    def _on_close(self) -> None:
        self._buffered = []
        self._left_row = None
        self._right_matched = set()

    def _produce(self) -> Row | None:
        if self._draining:
            return self._unmatched_right()

        while True:
            if self._left_row is None:
                self._left_row = self._left.next()
                if self._left_row is None:
                    # The left input is done, which is the first moment
                    # "unmatched" is finally known for the right side. Rewind the
                    # cursor — it is sitting at the end of the buffer from the last
                    # left row — and hand out the leftovers one per call.
                    self._draining = True
                    self._position = 0
                    return self._unmatched_right()
                self.stats.input_rows += 1
                self._position = 0
                self._left_matched = False

            while self._position < len(self._buffered):
                index = self._position
                right = self._buffered[index]
                self._position += 1
                merged = self._merge(self._left_row, right)
                if self._matches(merged):
                    self._left_matched = True
                    self._right_matched.add(index)
                    return merged

            unmatched = self._left_row
            self._left_row = None
            if self._preserve_left and not self._left_matched:
                # Emitted unchanged: the right side's slots in this row have never
                # been written, so it is already NULL-extended.
                return unmatched

    def _unmatched_right(self) -> Row | None:
        """Right rows that never matched, one per call, then ``None`` forever."""
        if not self._preserve_right:
            return None
        while self._position < len(self._buffered):
            index = self._position
            self._position += 1
            if index not in self._right_matched:
                return self._buffered[index]
        return None


class HashJoin(JoinOperator):
    """Build a hash table on the left, probe it with the right.

    ``O(n + m)`` instead of ``O(n·m)``, paid for in memory proportional to the
    build side. The planner builds on the *smaller* estimate; getting that
    backwards is the difference between a table of ten rows and one of ten
    million, and it is why the cost model needs a row count for each side and
    not just for the result.

    Only one equality is hashed. ``a.x = b.y AND a.z < b.w`` hashes the first
    and re-checks the second per matching pair, which is what ``residual`` is —
    a composite hash key would need the same key encoding a composite index
    would, and :mod:`engine.index.key` explains why that is a whole layer.

    NULL never matches, including another NULL. That is what ``=`` means in
    three-valued logic and a hash table would happily match them, so a NULL-keyed
    row is kept out of the table — but for an outer join it must still be
    **emitted**, because a row that cannot match is the definition of unmatched.
    Dropping it was correct only while every join was inner, and it is the one
    place where the outer version is not simply "the same, plus leftovers".

    Preserving the two sides costs different things. The *probe* side is nearly
    free: a probe row that finds no bucket is emitted on the spot. The *build*
    side needs the rows kept in insertion order as well as in buckets, a set of
    which ones were ever matched, and a pass over the leftovers once the probe
    input runs dry.
    """

    __slots__ = (
        "_bucket",
        "_build",
        "_build_key",
        "_build_matched",
        "_drain",
        "_draining",
        "_position",
        "_probe_key",
        "_probe_matched",
        "_probe_row",
        "_residual",
        "_table",
    )

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        left: Operator,
        right: Operator,
        predicate: Expression,
        build_key: Expression,
        probe_key: Expression,
        residual: Expression | None,
        right_slices: tuple[tuple[int, int], ...],
        preserve_left: bool = False,
        preserve_right: bool = False,
    ) -> None:
        super().__init__(
            operator_id,
            context,
            left=left,
            right=right,
            predicate=predicate,
            right_slices=right_slices,
            preserve_left=preserve_left,
            preserve_right=preserve_right,
        )
        self._build_key = build_key
        self._probe_key = probe_key
        self._residual = residual
        self._build: list[Row] = []
        """Every build row in insertion order, so an unmatched one can be found
        again — and so the buckets can hold indices rather than rows. The rows
        themselves are shared, so this costs one integer per row over holding them
        in the buckets directly, which is what buys a preserved build side."""
        self._table: dict[Any, list[int]] = {}
        """Key to *indices* into ``_build``, not to rows. Two identical build rows
        are two rows; a set of row values would lose one of them."""
        self._build_matched: set[int] = set()
        self._bucket: Sequence[int] = ()
        self._probe_row: Row = ()
        self._position = 0
        self._drain = 0
        self._draining = False
        self._probe_matched = False
        """Whether the probe row in hand has matched *anything* yet.

        Not the same question as "is its bucket empty". A bucket can be full and
        every candidate in it rejected by the residual — ``ON a.id = b.id AND
        b.y > 0`` hashes the equality and re-checks the rest per pair — and such a
        probe row is unmatched however promising its key looked."""

    @property
    def detail(self) -> str:
        label = (
            "FULL"
            if self._preserve_left and self._preserve_right
            else "LEFT"
            if self._preserve_left
            else "RIGHT"
            if self._preserve_right
            else ""
        )
        core = (
            f"{describe_expression(self._build_key)} = "
            f"{describe_expression(self._probe_key)}"
        )
        rest = f" AND {describe_expression(self._residual)}" if self._residual else ""
        return (f"{label} " if label else "") + core + rest

    def _key(self, row: Row, expression: Expression) -> Any:
        return evaluate(
            expression, row, tracer=self.context.tracer, operator_id=self.operator_id
        )

    def _on_open(self) -> None:
        self._table = {}
        self._build = []
        self._build_matched = set()
        for row in self._left:
            index = len(self._build)
            self._build.append(row)
            key = self._key(row, self._build_key)
            if key is None:
                # Out of the table: NULL = NULL is UNKNOWN, not TRUE, and hashing
                # it would match. It stays in `_build`, so a preserved build side
                # still emits it as unmatched — which it certainly is.
                continue
            self._table.setdefault(key, []).append(index)
        self._bucket = ()
        self._position = 0
        self._drain = 0
        self._draining = False
        self._probe_matched = False

    def _on_close(self) -> None:
        self._table = {}
        self._build = []
        self._build_matched = set()
        self._bucket = ()

    def _produce(self) -> Row | None:
        while True:
            while self._position < len(self._bucket):
                index = self._bucket[self._position]
                self._position += 1
                merged = self._merge(self._build[index], self._probe_row)
                if self._residual is None or self._matches(merged):
                    self._probe_matched = True
                    if self._preserve_left:
                        self._build_matched.add(index)
                    return merged

            # The bucket is exhausted. If nothing in it survived the residual then
            # this probe row never matched, whatever its key hashed to.
            if self._preserve_right and self._bucket and not self._probe_matched:
                self._bucket = ()
                return self._probe_row

            if self._draining:
                return self._unmatched_build()

            probe = self._right.next()
            if probe is None:
                self._draining = True
                continue
            self.stats.input_rows += 1
            key = self._key(probe, self._probe_key)
            bucket = () if key is None else self._table.get(key, ())
            if not bucket:
                # No partner, and a NULL key cannot acquire one. Either way this
                # probe row is unmatched: emitted as-is if the probe side is
                # preserved, dropped if not.
                if self._preserve_right:
                    return probe
                continue
            self._probe_row = probe
            self._bucket = bucket
            self._position = 0
            self._probe_matched = False

    def _unmatched_build(self) -> Row | None:
        """Build rows that never matched, one per call, then ``None`` forever."""
        if not self._preserve_left:
            return None
        while self._drain < len(self._build):
            index = self._drain
            self._drain += 1
            if index not in self._build_matched:
                return self._build[index]
        return None


class HashAggregate(Operator):
    """One row per group. The first operator here that is not a pipeline.

    Every input row is read before the first output row exists, because a group
    is not complete until the input is. That shows up in the plan view as the
    point where "time to first row" stops being small, and it is the honest
    reason ``LIMIT 1`` over a ``GROUP BY`` saves nothing.

    Hashing rather than sorting: linear in rows, independent of the number of
    groups, and it buys no ordering — which is exactly right, because nobody
    asked for one. PostgreSQL picks between ``HashAggregate`` and
    ``GroupAggregate`` on whether the input is already sorted; nothing here
    produces sorted input, so there is no choice to make.
    """

    __slots__ = ("_aggregates", "_child", "_group_keys", "_having", "_output", "_position")

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        child: Operator,
        group_keys: tuple[Expression, ...],
        aggregates: tuple[BoundAggregate, ...],
        having: Expression | None,
    ) -> None:
        super().__init__(operator_id, context)
        self._child = child
        self._group_keys = group_keys
        self._aggregates = aggregates
        self._having = having
        self._output: list[Row] = []
        self._position = 0

    @property
    def children(self) -> tuple[Operator, ...]:
        return (self._child,)

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return tuple(
            ResultColumn(describe_expression(key), None) for key in self._group_keys
        ) + tuple(ResultColumn(entry.label, None) for entry in self._aggregates)

    @property
    def detail(self) -> str:
        keys = ", ".join(describe_expression(key) for key in self._group_keys)
        return f"by {keys or 'all rows'}"

    def _on_open(self) -> None:
        groups: dict[tuple[Any, ...], list[_Accumulator]] = {}
        order: list[tuple[Any, ...]] = []

        for row in self._child:
            self.stats.input_rows += 1
            key = tuple(
                evaluate(
                    expression,
                    row,
                    tracer=self.context.tracer,
                    operator_id=self.operator_id,
                )
                for expression in self._group_keys
            )
            accumulators = groups.get(key)
            if accumulators is None:
                accumulators = [_Accumulator(entry.function) for entry in self._aggregates]
                groups[key] = accumulators
                order.append(key)
            for entry, accumulator in zip(self._aggregates, accumulators, strict=True):
                accumulator.add(
                    None
                    if entry.counts_rows
                    else evaluate(
                        entry.argument,
                        row,
                        tracer=self.context.tracer,
                        operator_id=self.operator_id,
                    ),
                    counts_rows=entry.counts_rows,
                )

        if not self._group_keys and not groups:
            # `SELECT COUNT(*) FROM empty` is 0, not no rows. There is exactly
            # one group when there are no keys, and it exists whether or not
            # anything fell into it. Add a GROUP BY and the same query over the
            # same empty table correctly returns nothing at all.
            groups[()] = [_Accumulator(entry.function) for entry in self._aggregates]
            order.append(())

        self._output = []
        for key in order:
            row = (*key, *(accumulator.value() for accumulator in groups[key]))
            if self._having is not None and not is_true(
                evaluate(
                    self._having,
                    row,
                    tracer=self.context.tracer,
                    operator_id=self.operator_id,
                ),
                clause="HAVING",
            ):
                continue
            self._output.append(row)
        self._position = 0

    def _on_close(self) -> None:
        self._output = []

    def _produce(self) -> Row | None:
        if self._position >= len(self._output):
            return None
        row = self._output[self._position]
        self._position += 1
        return row


class _Accumulator:
    """One aggregate's running state.

    ``COUNT`` counts; everything else ignores NULL, which is the rule that makes
    ``AVG`` of ``[1, NULL, 3]`` equal 2 and not 1.33 — the NULL is not a zero,
    it is a row that does not participate. ``SUM`` and ``AVG`` over no non-NULL
    values are NULL, and only ``COUNT`` is 0.

    The accumulator no longer has to ask what it is adding: the binder has
    already refused ``SUM`` and ``AVG`` over anything but a number
    (:func:`~engine.executor.binder._require_aggregable`). It does still have to
    ask how *big* the total has grown, because Python's integers do not overflow
    and ``INTEGER`` does — a sum of int64 values need not be one.
    """

    __slots__ = ("_count", "_function", "_max", "_min", "_sum")

    def __init__(self, function: AggregateFunction) -> None:
        self._function = function
        self._count = 0
        self._sum: Any = None
        self._min: Any = None
        self._max: Any = None

    def add(self, value: Any, *, counts_rows: bool) -> None:
        if counts_rows:
            self._count += 1
            return
        if value is None:
            return
        self._count += 1
        total = value if self._sum is None else self._sum + value
        self._sum = check_numeric_range(total, "SUM")
        self._min = value if self._min is None else min(self._min, value)
        self._max = value if self._max is None else max(self._max, value)

    def value(self) -> Any:
        match self._function:
            case AggregateFunction.COUNT:
                return self._count
            case AggregateFunction.SUM:
                return self._sum
            case AggregateFunction.AVG:
                # Always a float, including over integers: the average of 1 and
                # 2 is 1.5, and truncating it because the column was INTEGER is
                # the kind of quiet wrongness this project exists to avoid.
                return None if self._count == 0 else self._sum / self._count
            case AggregateFunction.MIN:
                return self._min
            case AggregateFunction.MAX:
                return self._max
        return None  # pragma: no cover - the enum is exhausted above


class Sort(Operator):
    """Buffer everything, order it, then emit. Blocking, like the aggregate.

    NULLs sort last ascending and first descending, which is PostgreSQL's rule
    (``NULLS LAST`` by default for ``ASC``) and the opposite of SQLite's. There
    is no right answer — the standard leaves it implementation-defined — but
    there is a wrong one, which is comparing NULL to a number and crashing.

    In memory only. A sort that does not fit is a sort that fails; PostgreSQL
    switches to an external merge at ``work_mem``. The row ceiling is what
    bounds this, and it is checked before the sort rather than during it.
    """

    __slots__ = ("_child", "_keys", "_position", "_rows")

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        child: Operator,
        keys: tuple[BoundSortKey, ...],
    ) -> None:
        super().__init__(operator_id, context)
        self._child = child
        self._keys = keys
        self._rows: list[Row] = []
        self._position = 0

    @property
    def children(self) -> tuple[Operator, ...]:
        return (self._child,)

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return self._child.output_columns

    @property
    def detail(self) -> str:
        return ", ".join(
            f"#{key.output_index}{' DESC' if key.descending else ''}" for key in self._keys
        )

    def _on_open(self) -> None:
        self._rows = list(self._child)
        self.stats.input_rows = len(self._rows)
        # One pass per key, least significant first. Python's sort is stable,
        # so the earlier keys survive — which is both correct and shorter than
        # a comparator that has to mix ascending and descending in one pass.
        for key in reversed(self._keys):
            self._rows.sort(
                key=lambda row, index=key.output_index: _sort_key(row[index]),
                reverse=key.descending,
            )
        self._position = 0

    def _on_close(self) -> None:
        self._rows = []

    def _produce(self) -> Row | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row


def _sort_key(value: Any) -> tuple[int, Any]:
    """Sort NULLs after everything, and never compare one to a number.

    The leading integer is the whole trick: it partitions before the value is
    ever reached, so ``None < 5`` is never evaluated. ``reverse=True`` flips the
    partition along with the values, which is why descending puts NULLs first —
    PostgreSQL's rule, arrived at by not having a special case rather than by
    having one.
    """
    if value is None:
        return (1, 0)
    if isinstance(value, bool):
        return (0, int(value))
    return (0, value)


class Limit(Operator):
    """Stop pulling after enough rows, having skipped ``offset``.

    The one operator that makes the pipeline visible: over a scan it really does
    stop early, and ``pages read`` in the plan view falls. Over a sort it saves
    nothing, because the sort already drained its child — and that non-saving is
    exactly as informative as the saving.
    """

    __slots__ = ("_child", "_count", "_emitted", "_offset", "_skipped")

    def __init__(
        self,
        operator_id: str,
        context: ExecutionContext,
        *,
        child: Operator,
        count: int,
        offset: int = 0,
    ) -> None:
        super().__init__(operator_id, context)
        self._child = child
        self._count = count
        self._offset = offset
        self._emitted = 0
        self._skipped = 0

    @property
    def children(self) -> tuple[Operator, ...]:
        return (self._child,)

    @property
    def output_columns(self) -> tuple[ResultColumn, ...]:
        return self._child.output_columns

    @property
    def detail(self) -> str:
        return f"{self._count}" + (f" offset {self._offset}" if self._offset else "")

    def _on_open(self) -> None:
        self._emitted = 0
        self._skipped = 0

    def _produce(self) -> Row | None:
        while self._skipped < self._offset:
            if self._child.next() is None:
                return None
            self._skipped += 1
        if self._emitted >= self._count:
            return None
        row = self._child.next()
        if row is None:
            return None
        self._emitted += 1
        return row


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
