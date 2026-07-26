"""Turning a statement into an answer.

    SQL text ──parse──▶ AST ──bind──▶ bound statement ──plan──▶ operator tree
                                                                    │
                                                              pull rows
                                                                    ▼
                                                              QueryResult

Milestone 3 has no cost-based planner: there is exactly one way to execute a
``SELECT``, because there are no indexes and no join orders to choose between.
:func:`build_select_plan` is therefore a *rule*-based translation, and the one
decision it makes — dropping a projection that returns every column unchanged —
is the seed of the real optimiser in Milestone 6.

``CREATE TABLE`` and ``INSERT`` are not planned at all. They have no operator
tree; they are direct calls into the storage engine. Modelling a single-row
insert as an operator pipeline would be structure for its own sake — though
Milestone 3's ``INSERT ... SELECT`` limitation is exactly what would justify it
later.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.diagnostics.events import QueryExecutedEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import (
    BindingError,
    CatalogError,
    ExecutionError,
    QueryCancelledError,
)
from engine.executor.binder import (
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
    Operator,
    Project,
    SeqScan,
)
from engine.parser.ast import (
    CreateTableStatement,
    InsertStatement,
    SelectStatement,
    Statement,
)
from engine.parser.parser import parse
from engine.serialization.record import Row
from engine.storage.heap import RecordId

if TYPE_CHECKING:
    from engine.database import Database

__all__ = [
    "ExecutionStats",
    "QueryResult",
    "build_select_plan",
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

    Always ``SeqScan`` at the leaf: with no indexes, a full scan is the only
    access path. Milestone 5 adds an alternative, and Milestone 6 adds the cost
    model that chooses between them.
    """
    heap = database.heap_for(bound.table_name)

    plan: Operator = SeqScan(
        "scan_1",
        context,
        heap=heap,
        schema=bound.input_schema,
        table_name=bound.table_name,
    )

    if bound.where is not None:
        plan = Filter("filter_1", context, child=plan, predicate=bound.where)

    # Skip a projection that would copy every column unchanged. The one
    # rule-based rewrite this milestone makes, and a real saving: it removes a
    # method call and a tuple build per row.
    if not bound.is_identity_projection:
        plan = Project(
            "project_1",
            context,
            child=plan,
            projections=bound.projections,
            output_columns=bound.output_columns,
        )

    return plan


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


def _find_scan(plan: Operator) -> SeqScan | None:
    for operator in _walk(plan):
        if isinstance(operator, SeqScan):
            return operator
    return None
