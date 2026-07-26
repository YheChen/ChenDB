"""Turning a statement into an answer.

    SQL text ──parse──▶ AST ──bind──▶ bound statement ──plan──▶ operator tree
                                                                    │
                                                              pull rows
                                                                    ▼
                                                              QueryResult

Planning is still *rule*-based, but Milestone 5 gives it something to decide.
With a B+ tree available there are two ways to read a table, and
:func:`choose_access_path` picks between them::

    WHERE age = 30            index on age?  →  IndexScan, key = 30
    WHERE age >= 20 AND …     index on age?  →  IndexScan, key >= 20
                                                 + Filter for the rest
    WHERE name LIKE 'a%'      no index        →  SeqScan + Filter

The rule is "use an index if one covers a comparison", which is the crude
version.  It never asks *how many rows will match*, and that is the question
that actually matters: an index scan pays one random heap read per matching row,
so above a few percent selectivity it loses to a sequential scan that reads each
page once.  Estimating selectivity, and therefore choosing correctly, is
Milestone 6.

``CREATE TABLE``, ``CREATE INDEX`` and ``INSERT`` are not planned at all. They
have no operator tree; they are direct calls into the storage engine. Modelling a
single-row insert as an operator pipeline would be structure for its own sake —
though the ``INSERT ... SELECT`` limitation is exactly what would justify it
later.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.catalog.catalog import IndexInfo
from engine.diagnostics.events import QueryExecutedEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import (
    BindingError,
    CatalogError,
    ExecutionError,
    IndexingError,
    QueryCancelledError,
)
from engine.executor.binder import (
    BoundColumnRef,
    BoundInsert,
    BoundSelect,
    ResultColumn,
    bind_create_table,
    bind_insert,
    bind_select,
)
from engine.executor.controller import NULL_CONTROLLER, StepController
from engine.executor.expression import evaluate
from engine.executor.operators import (
    ExecutionContext,
    Filter,
    IndexScan,
    Operator,
    Project,
    ScanOperator,
    SeqScan,
)
from engine.index.key import SMALLEST_VALUE_KEY
from engine.parser.ast import (
    BinaryOp,
    BinaryOperator,
    CreateIndexStatement,
    CreateTableStatement,
    Expression,
    InsertStatement,
    Literal,
    SelectStatement,
    Statement,
)
from engine.parser.parser import parse
from engine.serialization.record import Row
from engine.storage.heap import RecordId

if TYPE_CHECKING:
    from engine.database import Database

__all__ = [
    "AccessPath",
    "ExecutionStats",
    "QueryResult",
    "build_select_plan",
    "choose_access_path",
    "execute_script",
    "execute_statement",
]

#: Ceiling on rows a single query returns, so an API caller cannot ask for an
#: unbounded response. ``LIMIT`` would make this configurable per query.
DEFAULT_MAX_ROWS = 10_000


@dataclass(slots=True)
class ExecutionStats:
    """What running a statement cost."""

    rows_returned: int = 0
    rows_affected: int = 0
    rows_scanned: int = 0
    rows_rejected: int = 0
    pages_read: int = 0
    pages_written: int = 0
    duration_ns: int = 0
    truncated: bool = False
    """True when the row ceiling was hit, so the result is incomplete."""

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "rows_returned": self.rows_returned,
            "rows_affected": self.rows_affected,
            "rows_scanned": self.rows_scanned,
            "rows_rejected": self.rows_rejected,
            "pages_read": self.pages_read,
            "pages_written": self.pages_written,
            "duration_ns": self.duration_ns,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class QueryResult:
    """The outcome of one statement."""

    statement_kind: str
    columns: tuple[ResultColumn, ...] = ()
    rows: tuple[Row, ...] = ()
    record_ids: tuple[RecordId, ...] = ()
    """Where each returned row lives. Empty for a projection that computes values."""
    stats: ExecutionStats = field(default_factory=ExecutionStats)
    plan: Operator | None = None
    """The operator tree, kept after execution so its statistics can be read."""
    cancelled: bool = False
    message: str = ""
    """Human-readable summary for statements that return no rows."""

    @property
    def returns_rows(self) -> bool:
        return self.statement_kind == "SelectStatement"


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def build_select_plan(
    bound: BoundSelect, database: Database, context: ExecutionContext
) -> Operator:
    """Build the operator tree for a bound ``SELECT``.

    Two access paths now exist, so this makes its first real choice. The rule is
    deliberately crude: **use an index whenever one covers a comparison in the
    ``WHERE`` clause**, otherwise scan. It never asks how many rows will match,
    which is the question that actually decides whether an index helps — see
    :class:`~engine.executor.operators.IndexScan` on why a badly chosen index
    scan is slower than a sequential one. Milestone 6 adds the cost model.
    """
    heap = database.heap_for(bound.table_name)
    access = choose_access_path(bound, database)

    plan: Operator
    if access is None:
        plan = SeqScan(
            "scan_1",
            context,
            heap=heap,
            schema=bound.input_schema,
            table_name=bound.table_name,
        )
        residual = bound.where
    else:
        plan = IndexScan(
            "scan_1",
            context,
            heap=heap,
            schema=bound.input_schema,
            table_name=bound.table_name,
            tree=database.tree_for(access.index_name),
            low=access.low,
            high=access.high,
            include_low=access.include_low,
            include_high=access.include_high,
        )
        residual = access.residual

    if residual is not None:
        plan = Filter("filter_1", context, child=plan, predicate=residual)

    # Skip a projection that would copy every column unchanged. A real saving:
    # it removes a method call and a tuple build per row.
    if not bound.is_identity_projection:
        plan = Project(
            "project_1",
            context,
            child=plan,
            projections=bound.projections,
            output_columns=bound.output_columns,
        )

    return plan


@dataclass(frozen=True, slots=True)
class AccessPath:
    """An index scan the planner decided it can use.

    ``residual`` is what the index could *not* express and still has to be
    filtered per row. Splitting the predicate this way is what ``EXPLAIN`` shows
    as "Index Cond" against "Filter" in PostgreSQL, and the distinction matters:
    an index condition bounds how much is read, a filter only throws away what
    was already read.
    """

    index_name: str
    low: bytes | None
    high: bytes | None
    include_low: bool
    include_high: bool
    residual: Expression | None


def choose_access_path(bound: BoundSelect, database: Database) -> AccessPath | None:
    """Pick an index for ``bound``'s ``WHERE`` clause, or ``None`` to scan.

    Handles a conjunction of comparisons against one indexed column, so
    ``age >= 20 AND age <= 30 AND name = 'x'`` becomes a bounded index scan on
    ``age`` with ``name = 'x'`` left as a residual filter. Anything else — an
    ``OR``, a comparison between two columns, a non-indexed column — falls
    through to a sequential scan rather than being half-handled.
    """
    if bound.where is None:
        return None

    conjuncts = _split_conjunction(bound.where)
    comparisons = [
        (index, comparison)
        for index, comparison in enumerate(conjuncts)
        if _as_column_comparison(comparison) is not None
    ]
    if not comparisons:
        return None

    # Group the usable comparisons by which column they constrain, then take the
    # first column that has an index. "First" is arbitrary and is precisely the
    # decision a cost model would make properly.
    for position in dict.fromkeys(
        _as_column_comparison(comparison)[0].column_index  # type: ignore[index]
        for _, comparison in comparisons
    ):
        for index_info in database.catalog.indexes_on(bound.table_name, position):
            bounds = _bounds_for(
                [entry for entry in comparisons if _column_of(entry[1]) == position],
                index_info,
            )
            if bounds is None:
                continue
            low, high, include_low, include_high, consumed = bounds
            residual = _rebuild_conjunction(
                [
                    conjunct
                    for position_in_list, conjunct in enumerate(conjuncts)
                    if position_in_list not in consumed
                ]
            )
            return AccessPath(
                index_name=index_info.name,
                low=low,
                high=high,
                include_low=include_low,
                include_high=include_high,
                residual=residual,
            )
    return None


def _split_conjunction(expression: Expression) -> list[Expression]:
    """Flatten ``a AND b AND c`` into ``[a, b, c]``. Anything else is one term."""
    if isinstance(expression, BinaryOp) and expression.operator is BinaryOperator.AND:
        return _split_conjunction(expression.left) + _split_conjunction(expression.right)
    return [expression]


def _rebuild_conjunction(terms: list[Expression]) -> Expression | None:
    """Re-join the terms an index could not absorb, left-associatively."""
    if not terms:
        return None
    combined = terms[0]
    for term in terms[1:]:
        combined = BinaryOp(
            node_id=term.node_id,
            span=combined.span.union(term.span),
            operator=BinaryOperator.AND,
            left=combined,
            right=term,
        )
    return combined


def _as_column_comparison(
    expression: Expression,
) -> tuple[BoundColumnRef, BinaryOperator, Any] | None:
    """Match ``column <op> literal`` (either way round), or return ``None``.

    A reversed comparison has its operator mirrored — ``18 < age`` constrains
    ``age`` from below, not above — which is the kind of detail that silently
    returns wrong rows if it is got wrong.
    """
    if not isinstance(expression, BinaryOp) or not expression.operator.is_comparison:
        return None
    if expression.operator is BinaryOperator.NEQ:
        # An index cannot bound `<>`: it excludes one key and admits every
        # other, so the scan would read the whole tree and the heap fetches
        # would make it strictly worse than a sequential scan.
        return None

    left, right = expression.left, expression.right
    if isinstance(left, BoundColumnRef) and isinstance(right, Literal):
        return left, expression.operator, right.value
    if isinstance(right, BoundColumnRef) and isinstance(left, Literal):
        return right, _MIRRORED[expression.operator], left.value
    return None


_MIRRORED: dict[BinaryOperator, BinaryOperator] = {
    BinaryOperator.EQ: BinaryOperator.EQ,
    BinaryOperator.LT: BinaryOperator.GT,
    BinaryOperator.LTE: BinaryOperator.GTE,
    BinaryOperator.GT: BinaryOperator.LT,
    BinaryOperator.GTE: BinaryOperator.LTE,
}


def _column_of(expression: Expression) -> int | None:
    matched = _as_column_comparison(expression)
    return matched[0].column_index if matched else None


def _bounds_for(
    candidates: list[tuple[int, Expression]], index_info: IndexInfo
) -> tuple[bytes | None, bytes | None, bool, bool, set[int]] | None:
    """Fold comparisons on one column into a single ``[low, high]`` range.

    Returns ``None`` when nothing usable survives — an incomparable literal type,
    or a comparison against ``NULL``, which three-valued logic makes false for
    every row and which therefore must not be turned into a range that would
    match the NULL keys in the tree.
    """
    low: bytes | None = None
    high: bytes | None = None
    include_low = True
    include_high = True
    consumed: set[int] = set()

    for position, comparison in candidates:
        matched = _as_column_comparison(comparison)
        assert matched is not None
        _, operator, value = matched
        if value is None:
            continue  # `x = NULL` is never true; let the filter handle it
        try:
            key = index_info.encode(value)
        except IndexingError:
            continue  # literal of a type this index cannot encode

        match operator:
            case BinaryOperator.EQ:
                low = high = key
                include_low = include_high = True
            case BinaryOperator.GT | BinaryOperator.GTE:
                inclusive = operator is BinaryOperator.GTE
                if low is None or key > low or (key == low and not inclusive):
                    low, include_low = key, inclusive
            case BinaryOperator.LT | BinaryOperator.LTE:
                inclusive = operator is BinaryOperator.LTE
                if high is None or key < high or (key == high and not inclusive):
                    high, include_high = key, inclusive
            case _:  # pragma: no cover - filtered out by _as_column_comparison
                continue
        consumed.add(position)

    if not consumed:
        return None

    # A range with no lower bound would sweep up the NULL keys, which sort below
    # every value — and no comparison is ever true for NULL. Anchoring at the
    # smallest possible *value* key excludes them, which is why the key encoding
    # gives NULL its own tag rather than treating it as a small value.
    if low is None:
        low, include_low = SMALLEST_VALUE_KEY, True
    return low, high, include_low, include_high, consumed


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def execute_statement(
    statement: Statement,
    database: Database,
    *,
    tracer: Tracer | None = None,
    controller: StepController | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> QueryResult:
    """Bind and run one statement against ``database``."""
    tracer = tracer if tracer is not None else NULL_TRACER
    context = ExecutionContext(
        tracer=tracer,
        controller=controller if controller is not None else NULL_CONTROLLER,
        max_rows=max_rows,
    )

    started = time.perf_counter_ns()
    reads_before = database.stats.page_reads
    writes_before = database.stats.page_writes
    context.controller.mark_running()

    match statement:
        case CreateTableStatement():
            result = _execute_create_table(statement, database)
        case CreateIndexStatement():
            result = _execute_create_index(statement, database)
        case InsertStatement():
            result = _execute_insert(statement, database, context)
        case SelectStatement():
            result = _execute_select(statement, database, context)
        case _:
            raise ExecutionError(f"cannot execute {statement.node_type}")

    result.stats.duration_ns = time.perf_counter_ns() - started
    result.stats.pages_read = database.stats.page_reads - reads_before
    result.stats.pages_written = database.stats.page_writes - writes_before

    if tracer.summary:
        tracer.emit(
            QueryExecutedEvent(
                statement_kind=result.statement_kind,
                rows_returned=result.stats.rows_returned,
                rows_affected=result.stats.rows_affected,
                duration_ns=result.stats.duration_ns,
                cancelled=result.cancelled,
            )
        )
    return result


def _execute_create_table(
    statement: CreateTableStatement, database: Database
) -> QueryResult:
    name, schema = bind_create_table(statement)

    existing = database.table(name)
    if existing is not None:
        if statement.if_not_exists:
            return QueryResult(
                statement_kind=statement.node_type,
                message=f"table {name!r} already exists, left unchanged",
            )
        raise BindingError(
            f"table {name!r} already exists",
            start=statement.table.span.start,
            end=statement.table.span.end,
            line=statement.table.span.line,
            column=statement.table.span.column,
        )

    try:
        info = database.create_table(name, schema)
    except CatalogError as exc:
        # A reserved name, for instance. The catalog carries no source position,
        # so attach the one from the statement.
        raise BindingError(
            str(exc),
            start=statement.table.span.start,
            end=statement.table.span.end,
            line=statement.table.span.line,
            column=statement.table.span.column,
        ) from None

    return QueryResult(
        statement_kind=statement.node_type,
        message=f"created table {info.name} with {len(schema)} column(s)",
    )


def _execute_create_index(
    statement: CreateIndexStatement, database: Database
) -> QueryResult:
    existing = database.index(statement.index_name)
    if existing is not None:
        if statement.if_not_exists:
            return QueryResult(
                statement_kind=statement.node_type,
                message=f"index {statement.index_name!r} already exists, left unchanged",
            )
        raise BindingError(
            f"index {statement.index_name!r} already exists",
            start=statement.span.start,
            end=statement.span.end,
            line=statement.span.line,
            column=statement.span.column,
        )

    try:
        index = database.create_index(
            statement.index_name,
            statement.table.name,
            statement.column,
            unique=statement.unique,
        )
    except (CatalogError, IndexingError) as exc:
        # Includes a UniqueViolation from a column that already holds duplicates,
        # which is the one failure a user is most likely to hit.
        raise BindingError(
            str(exc),
            start=statement.span.start,
            end=statement.span.end,
            line=statement.span.line,
            column=statement.span.column,
        ) from None

    tree = database.tree_for(index.name)
    kind = "unique index" if index.unique else "index"
    return QueryResult(
        statement_kind=statement.node_type,
        message=(
            f"created {kind} {index.name} on {index.table_name}({index.column_name}); "
            f"{tree.count()} entries, height {tree.height}"
        ),
    )


def _execute_insert(
    statement: InsertStatement, database: Database, context: ExecutionContext
) -> QueryResult:
    bound: BoundInsert = bind_insert(statement, database.catalog)

    # Every value is evaluated against an empty row: an INSERT's values are
    # constant expressions. `INSERT ... SELECT`, which would need a real input
    # operator, is rejected by the parser as not implemented.
    rows: list[Sequence[Any]] = [
        tuple(
            evaluate(expression, (), tracer=context.tracer, operator_id="insert_1")
            for expression in row
        )
        for row in bound.rows
    ]

    record_ids = database.insert_many(bound.table_name, rows)
    database.sync()

    stats = ExecutionStats(rows_affected=len(record_ids))
    return QueryResult(
        statement_kind=statement.node_type,
        record_ids=tuple(record_ids),
        stats=stats,
        message=f"inserted {len(record_ids)} row(s) into {bound.table_name}",
    )


def _execute_select(
    statement: SelectStatement, database: Database, context: ExecutionContext
) -> QueryResult:
    bound = bind_select(statement, database.catalog)
    plan = build_select_plan(bound, database, context)

    rows: list[Row] = []
    record_ids: list[RecordId] = []
    cancelled = False
    stats = ExecutionStats()
    scan = _find_scan(plan)
    limit = context.max_rows if context.max_rows is not None else DEFAULT_MAX_ROWS

    try:
        # open() is inside the try because it contains checkpoints too: a query
        # cancelled while its operators were still opening must unwind the same
        # way as one cancelled mid-scan, not propagate the exception.
        plan.open()
        while (row := plan.next()) is not None:
            rows.append(row)
            if scan is not None and scan.last_record_id is not None:
                record_ids.append(scan.last_record_id)
            if len(rows) >= limit:
                stats.truncated = True
                break
    except QueryCancelledError:
        # Not an error: the client asked to stop. Whatever was produced before
        # the cancellation is still a valid partial answer.
        cancelled = True
    finally:
        # close() unwinds the whole tree, releasing the scan's generator, on
        # every path including cancellation.
        plan.close()

    stats.rows_returned = len(rows)
    stats.rows_scanned = scan.stats.input_rows if scan else 0
    stats.rows_rejected = sum(
        operator.rows_rejected
        for operator in _walk(plan)
        if isinstance(operator, Filter)
    )

    return QueryResult(
        statement_kind=statement.node_type,
        columns=bound.output_columns,
        rows=tuple(rows),
        record_ids=tuple(record_ids),
        stats=stats,
        plan=plan,
        cancelled=cancelled,
    )


def execute_script(
    sql: str,
    database: Database,
    *,
    tracer: Tracer | None = None,
    controller: StepController | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list[QueryResult]:
    """Parse and run every statement in ``sql``, in order.

    There are no transactions yet, so a script that fails half-way leaves the
    statements before the failure applied. Milestone 8 makes that atomic.
    """
    results: list[QueryResult] = []
    for statement in parse(sql, tracer=tracer):
        results.append(
            execute_statement(
                statement,
                database,
                tracer=tracer,
                controller=controller,
                max_rows=max_rows,
            )
        )
    return results


# -- tree helpers ----------------------------------------------------------


def _walk(operator: Operator) -> list[Operator]:
    out = [operator]
    for child in operator.children:
        out.extend(_walk(child))
    return out


def _find_scan(plan: Operator) -> ScanOperator | None:
    """The leaf that reads the table, whichever access path was chosen."""
    for operator in _walk(plan):
        if isinstance(operator, ScanOperator):
            return operator
    return None
