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
single-row insert as an operator pipeline would be structure for its own sake,
though the ``INSERT ... SELECT`` limitation is exactly what would justify it
later.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from engine.diagnostics.events import (
    CostEstimateEvent,
    LogicalPlanEvent,
    PhysicalPlanEvent,
    PlanAlternativeEvent,
    QueryExecutedEvent,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import (
    BindingError,
    CatalogError,
    ExecutionError,
    IndexingError,
    QueryCancelledError,
    TransactionError,
    UnsupportedSqlError,
)
from engine.executor.binder import (
    BoundDelete,
    BoundInsert,
    BoundSelect,
    BoundUpdate,
    ResultColumn,
    bind_create_table,
    bind_delete,
    bind_insert,
    bind_select,
    bind_update,
    identity_projection,
)
from engine.executor.controller import NULL_CONTROLLER, StepController
from engine.executor.expression import evaluate
from engine.executor.operators import (
    ExecutionContext,
    Filter,
    HashAggregate,
    HashJoin,
    IndexScan,
    Limit,
    NestedLoopJoin,
    Operator,
    Project,
    ScanOperator,
    SeqScan,
    Sort,
)
from engine.parser.ast import (
    AnalyzeStatement,
    BeginStatement,
    BinaryOp,
    ColumnRef,
    CommitStatement,
    CreateIndexStatement,
    CreateTableStatement,
    DeleteStatement,
    ExplainStatement,
    Expression,
    FunctionCall,
    InsertStatement,
    IsNullTest,
    Literal,
    RollbackStatement,
    ScalarSubquery,
    SelectStatement,
    Star,
    Statement,
    UnaryOp,
    UpdateStatement,
    walk,
)
from engine.parser.parser import parse
from engine.planner.logical import (
    LogicalAggregate,
    LogicalFilter,
    LogicalJoin,
    LogicalLimit,
    LogicalNode,
    LogicalProject,
    LogicalScan,
    LogicalSort,
    walk_logical,
)
from engine.planner.physical import (
    DEFAULT_PLANNER_OPTIONS,
    PhysicalAggregate,
    PhysicalFilter,
    PhysicalHashJoin,
    PhysicalIndexScan,
    PhysicalLimit,
    PhysicalNestedLoopJoin,
    PhysicalNode,
    PhysicalProject,
    PhysicalSeqScan,
    PhysicalSort,
    PlannedQuery,
    PlannerOptions,
    describe_physical,
    plan_select,
    walk_physical,
)
from engine.serialization.record import Row
from engine.serialization.schema import Schema
from engine.serialization.types import DataType
from engine.storage.heap import RecordId
from engine.transaction.manager import TransactionState

if TYPE_CHECKING:
    from engine.database import Database

__all__ = [
    "ExecutionStats",
    "QueryResult",
    "build_logical_plan",
    "build_row_source",
    "build_select_plan",
    "execute_script",
    "execute_statement",
    "materialise",
    "plan_query",
    "plan_row_source",
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
    planned: PlannedQuery | None = None
    """What the planner decided, including the alternatives it rejected."""
    cancelled: bool = False
    message: str = ""
    """Human-readable summary for statements that return no rows."""

    @property
    def returns_rows(self) -> bool:
        return self.statement_kind in ("SelectStatement", "ExplainStatement")


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def build_logical_plan(bound: BoundSelect) -> LogicalNode:
    """Turn a bound ``SELECT`` into a logical plan.

    Straight structural translation with no decisions in it. Every decision
    belongs to a rewrite rule or to the cost model, where it can be named and
    inspected. Milestone 3 dropped an identity projection here; that is now
    :mod:`engine.optimizer.rules`.

    The nodes come out in **SQL's evaluation order**, which is not its written
    order, and reading the tree bottom-up is the shortest explanation of why
    ``ORDER BY`` can use a select-list alias and ``WHERE`` cannot::

        Limit                  LIMIT 10
          Sort                 ORDER BY total DESC
            Project            SELECT u.name, SUM(o.total) AS total
              Aggregate        GROUP BY u.name  HAVING SUM(o.total) > 100
                Filter         WHERE o.paid
                  Join         ON u.id = o.user_id
                    Scan users
                    Scan orders

    The joins are emitted left-deep in the order they were written. That is a
    *starting point*, not a decision: :func:`~engine.planner.physical.plan_select`
    re-derives the order from scratch.
    """
    scope = bound.scope
    total = scope.width

    plan: LogicalNode = LogicalScan(
        "scan_1",
        scope.entries[0].table_name,
        scope.entries[0].schema,
        position=0,
        offset=scope.entries[0].offset,
        total_width=total,
    )
    for index, join in enumerate(bound.joins, start=1):
        entry = scope.entry(join.binding_name)
        assert entry is not None
        plan = LogicalJoin(
            f"join_{index}",
            join.condition,
            plan,
            LogicalScan(
                f"scan_{entry.position + 1}",
                entry.table_name,
                entry.schema,
                position=entry.position,
                offset=entry.offset,
                total_width=total,
            ),
            kind=join.kind,
        )

    if bound.where is not None:
        plan = LogicalFilter("filter", bound.where, plan)

    if bound.aggregation is not None:
        plan = LogicalAggregate(
            "aggregate",
            bound.aggregation.group_keys,
            bound.aggregation.aggregates,
            bound.aggregation.having,
            plan,
        )

    plan = LogicalProject("project", bound.projections, bound.output_columns, plan)

    if bound.order_by:
        plan = LogicalSort("sort", bound.order_by, plan)
    if bound.limit is not None:
        plan = LogicalLimit("limit", bound.limit, bound.offset or 0, plan)
    return plan


def build_row_source(
    table_name: str,
    schema: Schema,
    where: Expression | None,
    projections: tuple[Expression, ...],
    output_columns: tuple[ResultColumn, ...],
) -> LogicalNode:
    """Scan, optionally filter, project. The shape every statement starts from.

    ``UPDATE`` and ``DELETE`` reach this too, because "which rows" is the same
    question whatever you then do with them, and it is the only question in
    either statement that has more than one answer. ``DELETE FROM t WHERE id =
    5`` on an indexed ``id`` should descend the tree; without this the planner
    would never see it, and a single-row delete would read the whole table.
    """
    plan: LogicalNode = LogicalScan("scan", table_name, schema)
    if where is not None:
        plan = LogicalFilter("filter", where, plan)
    return LogicalProject("project", projections, output_columns, plan)


def plan_query(
    bound: BoundSelect,
    database: Database,
    *,
    tracer: Tracer | None = None,
    options: PlannerOptions = DEFAULT_PLANNER_OPTIONS,
) -> PlannedQuery:
    """Bind → logical → rewrite → enumerate → cost → choose.

    The whole planner, in one call, returning everything it decided *and* what
    it rejected. Runs no I/O beyond gathering statistics, so ``EXPLAIN`` can
    call it without executing anything.
    """
    tracer = tracer if tracer is not None else NULL_TRACER
    logical = build_logical_plan(bound)
    planned = plan_select(logical, database, options)
    _emit_plan(planned, tracer)
    return planned


def plan_row_source(
    bound: BoundUpdate | BoundDelete,
    database: Database,
    *,
    tracer: Tracer | None = None,
    options: PlannerOptions = DEFAULT_PLANNER_OPTIONS,
) -> PlannedQuery:
    """:func:`plan_query` for the row-locating half of an ``UPDATE``/``DELETE``."""
    tracer = tracer if tracer is not None else NULL_TRACER
    projections, outputs = identity_projection(bound.schema, bound.statement)
    logical = build_row_source(
        bound.table_name, bound.schema, bound.where, projections, outputs
    )
    planned = plan_select(logical, database, options)
    _emit_plan(planned, tracer)
    return planned


def _emit_plan(planned: PlannedQuery, tracer: Tracer) -> None:
    if not tracer.operator:
        return
    tracer.emit(
        LogicalPlanEvent(
            table_name=planned.statistics.table_name,
            node_count=len(walk_logical(planned.logical)),
            rules_applied=", ".join(planned.rewrites),
        )
    )
    for alternative in planned.alternatives:
        tracer.emit(
            PlanAlternativeEvent(
                description=alternative.description,
                access_path=alternative.access_path,
                estimated_cost=round(alternative.cost.total, 2),
                estimated_rows=round(alternative.cost.rows, 1),
                chosen=alternative.chosen,
                rejected_because=alternative.rejected_because,
            )
        )
    tracer.emit(
        PhysicalPlanEvent(
            access_path=next(
                (a.access_path for a in planned.alternatives if a.chosen), "?"
            ),
            estimated_cost=round(planned.estimated_cost, 2),
            estimated_rows=round(planned.estimated_rows, 1),
            candidates_considered=len(planned.alternatives),
            statistics_stale=planned.statistics_are_stale,
        )
    )
    if tracer.verbose:
        for node in walk_physical(planned.root):
            tracer.emit(
                CostEstimateEvent(
                    node_id=node.node_id,
                    node_type=node.node_type,
                    io_cost=round(node.estimated.io, 2),
                    cpu_cost=round(node.estimated.cpu, 2),
                    estimated_rows=round(node.estimated.rows, 1),
                )
            )


def materialise(
    node: PhysicalNode, database: Database, context: ExecutionContext
) -> Operator:
    """Turn a costed physical plan into a running operator tree.

    Kept separate from planning so a plan can be built, compared and printed
    without opening an index or a heap, which is what lets ``EXPLAIN`` cost a
    query it never runs.
    """
    match node:
        case PhysicalSeqScan():
            return SeqScan(
                node.node_id,
                context,
                heap=database.heap_for(node.table_name),
                schema=node.schema,
                table_name=node.table_name,
                layout=node.layout,
            )

        case PhysicalIndexScan():
            return IndexScan(
                node.node_id,
                context,
                heap=database.heap_for(node.table_name),
                schema=node.schema,
                table_name=node.table_name,
                tree=database.tree_for(node.index_name),
                low=node.low,
                high=node.high,
                include_low=node.include_low,
                include_high=node.include_high,
                layout=node.layout,
            )

        case PhysicalNestedLoopJoin():
            return NestedLoopJoin(
                node.node_id,
                context,
                left=materialise(node.left, database, context),
                right=materialise(node.right, database, context),
                predicate=node.predicate,
                right_slices=node.right_slices,
                preserve_left=node.preserve_left,
                preserve_right=node.preserve_right,
            )

        case PhysicalHashJoin():
            return HashJoin(
                node.node_id,
                context,
                left=materialise(node.left, database, context),
                right=materialise(node.right, database, context),
                predicate=node.predicate,
                build_key=node.build_key,
                probe_key=node.probe_key,
                residual=node.residual,
                right_slices=node.right_slices,
                preserve_left=node.preserve_left,
                preserve_right=node.preserve_right,
            )

        case PhysicalAggregate():
            return HashAggregate(
                node.node_id,
                context,
                child=materialise(node.child, database, context),
                group_keys=node.group_keys,
                aggregates=node.aggregates,
                having=node.having,
            )

        case PhysicalSort():
            return Sort(
                node.node_id,
                context,
                child=materialise(node.child, database, context),
                keys=node.keys,
            )

        case PhysicalLimit():
            return Limit(
                node.node_id,
                context,
                child=materialise(node.child, database, context),
                count=node.count,
                offset=node.offset,
            )

        case PhysicalFilter():
            return Filter(
                node.node_id,
                context,
                child=materialise(node.child, database, context),
                predicate=node.predicate,
            )

        case PhysicalProject():
            return Project(
                node.node_id,
                context,
                child=materialise(node.child, database, context),
                projections=node.projections,
                output_columns=node.output_columns,
            )

    raise ExecutionError(f"cannot execute {node.node_type}")  # pragma: no cover


def build_select_plan(
    bound: BoundSelect, database: Database, context: ExecutionContext
) -> Operator:
    """Plan and materialise in one step. The path a plain ``SELECT`` takes."""
    planned = plan_query(
        bound,
        database,
        tracer=context.tracer,
        options=context.planner_options or DEFAULT_PLANNER_OPTIONS,
    )
    return materialise(planned.root, database, context)


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
    planner_options: PlannerOptions = DEFAULT_PLANNER_OPTIONS,
) -> QueryResult:
    """Bind and run one statement against ``database``."""
    tracer = tracer if tracer is not None else NULL_TRACER
    context = ExecutionContext(
        tracer=tracer,
        controller=controller if controller is not None else NULL_CONTROLLER,
        max_rows=max_rows,
        planner_options=planner_options,
        # Taken here, once per statement, and not inside the scan. Under READ
        # COMMITTED that *is* the per-statement snapshot; under REPEATABLE READ
        # the manager hands back the one the transaction already holds. Either
        # way both scans in a plan see the same database, which they would not
        # if each took its own.
        snapshot=database.snapshot(),
    )

    started = time.perf_counter_ns()
    reads_before = database.stats.page_reads
    writes_before = database.stats.page_writes
    context.controller.mark_running()
    _check_transaction_usable(statement, database)
    database.transactions.note_statement()

    try:
        match statement:
            case CreateTableStatement():
                result = _execute_create_table(statement, database)
            case CreateIndexStatement():
                result = _execute_create_index(statement, database)
            case AnalyzeStatement():
                result = _execute_analyze(statement, database)
            case BeginStatement() | CommitStatement() | RollbackStatement():
                result = _execute_transaction(statement, database)
            case ExplainStatement():
                result = _execute_explain(statement, database, context, max_rows)
            case InsertStatement():
                result = _execute_insert(statement, database, context)
            case UpdateStatement():
                result = _execute_update(statement, database, context)
            case DeleteStatement():
                result = _execute_delete(statement, database, context)
            case SelectStatement():
                result = _execute_select(statement, database, context)
            case _:
                raise ExecutionError(f"cannot execute {statement.node_type}")
    except Exception:
        # The transaction is now doomed, whether or not this call owns it.
        # ``execute_script`` will unwind one it opened itself; for one the
        # client opened in an earlier request, this is what stops a later
        # COMMIT from keeping the work that ran before the failure.
        database.transactions.mark_failed()
        raise

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


def _check_transaction_usable(statement: Statement, database: Database) -> None:
    """Refuse anything but COMMIT and ROLLBACK once a statement has failed.

    PostgreSQL's rule, and its wording. Without it the failed state would be
    advisory: a client could carry on inserting after an error and the
    transaction would look healthy again by the time it committed.
    """
    if not database.transactions.is_failed:
        return
    if isinstance(statement, CommitStatement | RollbackStatement):
        return
    raise BindingError(
        "current transaction is aborted, commands ignored until end of transaction block",
        start=statement.span.start,
        end=statement.span.end,
        line=statement.span.line,
        column=statement.span.column,
    )


def _execute_transaction(statement: Statement, database: Database) -> QueryResult:
    """``BEGIN`` / ``COMMIT`` / ``ROLLBACK``.

    A ``TransactionError`` becomes a positioned ``BindingError`` so the editor
    can underline the offending ``COMMIT`` rather than reporting a bare engine
    error with no idea where it came from.
    """
    try:
        match statement:
            case BeginStatement():
                transaction = database.begin()
                message = f"transaction {transaction.transaction_id} started"
            case CommitStatement():
                transaction = database.commit()
                if transaction.state is TransactionState.ABORTED:
                    # COMMIT after a failed statement is a rollback. Say so
                    # rather than reporting success for work that is gone.
                    message = (
                        f"transaction {transaction.transaction_id} rolled back: "
                        f"a statement in it failed "
                        f"({transaction.pages_restored} page(s) restored)"
                    )
                else:
                    message = (
                        f"transaction {transaction.transaction_id} committed "
                        f"({transaction.statements} statement(s), "
                        f"{transaction.pages_written} page write(s))"
                    )
            case _:
                transaction = database.rollback()
                message = (
                    f"transaction {transaction.transaction_id} rolled back "
                    f"({transaction.pages_restored} page(s) restored)"
                )
    except TransactionError as exc:
        raise BindingError(
            str(exc),
            start=statement.span.start,
            end=statement.span.end,
            line=statement.span.line,
            column=statement.span.column,
        ) from None

    return QueryResult(statement_kind=statement.node_type, message=message)


def _execute_analyze(statement: AnalyzeStatement, database: Database) -> QueryResult:
    name = statement.table.name if statement.table else None
    try:
        gathered = database.analyze(name)
    except CatalogError as exc:
        raise BindingError(
            str(exc),
            start=statement.span.start,
            end=statement.span.end,
            line=statement.span.line,
            column=statement.span.column,
        ) from None

    if not gathered:
        return QueryResult(
            statement_kind=statement.node_type,
            message="no tables to analyze",
        )
    summary = ", ".join(
        f"{stats.table_name} ({stats.row_count} rows)" for stats in gathered
    )
    return QueryResult(
        statement_kind=statement.node_type,
        message=f"analyzed {summary}",
    )


#: The columns EXPLAIN returns. Rows rather than a bespoke response shape, so
#: every client that can already display a SELECT can display an EXPLAIN,
#: which is exactly why PostgreSQL's EXPLAIN returns a one-column result set.
_EXPLAIN_COLUMNS: tuple[ResultColumn, ...] = (ResultColumn("QUERY PLAN", None),)


def _execute_explain(
    statement: ExplainStatement,
    database: Database,
    context: ExecutionContext,
    max_rows: int,
) -> QueryResult:
    """Plan the inner statement, optionally run it, and return the plan as rows.

    ``UPDATE`` and ``DELETE`` are explainable because the interesting half of
    each (finding the rows) *is* a plan, and it is the half where a missing
    index shows up. What comes after it is not planned and gets one honest line
    saying so, rather than a fabricated node with an invented cost.
    """
    inner = statement.statement
    options = context.planner_options or DEFAULT_PLANNER_OPTIONS
    epilogue: str | None = None

    match inner:
        case SelectStatement():
            # Folded here as well as in `_execute_select`, because an EXPLAIN
            # must show the plan the query would actually get, and after
            # folding that plan holds a literal rather than a subquery. It also
            # means EXPLAIN *runs* the subquery, which is worth knowing: an
            # uncorrelated subquery is a constant, and you cannot plan around a
            # constant you have not computed.
            inner = fold_subqueries(inner, database, context)
            planned = plan_query(
                bind_select(inner, database.catalog),
                database,
                tracer=context.tracer,
                options=options,
            )
        case UpdateStatement():
            planned = plan_row_source(
                bind_update(inner, database.catalog),
                database,
                tracer=context.tracer,
                options=options,
            )
            epilogue = (
                "then, per row: tombstone it, insert a new version, rewrite every index"
            )
        case DeleteStatement():
            planned = plan_row_source(
                bind_delete(inner, database.catalog),
                database,
                tracer=context.tracer,
                options=options,
            )
            epilogue = "then, per row: set xmax, remove it from every index"
        case _:
            raise BindingError(
                f"EXPLAIN can only explain a SELECT, UPDATE or DELETE, not "
                f"{inner.node_type}; nothing else locates rows",
                start=inner.span.start,
                end=inner.span.end,
                line=inner.span.line,
                column=inner.span.column,
            )

    actual: QueryResult | None = None
    if statement.analyze:
        # EXPLAIN ANALYZE runs the query. The row counts it reports are the real
        # ones, which is the only way to see where an estimate went wrong. For a
        # DELETE or UPDATE that means the rows really are changed: PostgreSQL
        # behaves the same way, and the usual advice applies: wrap it in a
        # transaction you intend to roll back.
        match inner:
            case SelectStatement():
                actual = _execute_select(inner, database, context, planned=planned)
            case UpdateStatement():
                actual = _execute_update(inner, database, context, planned=planned)
            case DeleteStatement():
                actual = _execute_delete(inner, database, context, planned=planned)

    prologue = (
        None
        if epilogue is None
        else f"{inner.node_type.removesuffix('Statement')} on {inner.table.name}"
    )
    lines = _explain_lines(planned, actual, prologue=prologue, epilogue=epilogue)
    return QueryResult(
        statement_kind=statement.node_type,
        columns=_EXPLAIN_COLUMNS,
        rows=tuple((line,) for line in lines[:max_rows]),
        plan=actual.plan if actual else None,
        planned=planned,
        stats=actual.stats if actual else ExecutionStats(rows_returned=len(lines)),
    )


def _explain_lines(
    planned: PlannedQuery,
    actual: QueryResult | None,
    *,
    prologue: str | None = None,
    epilogue: str | None = None,
) -> list[str]:
    """The plan as text, in the order PostgreSQL prints it.

    ``prologue`` and ``epilogue`` bracket the tree for a statement whose plan is
    only part of the work. The ``Delete on users`` header and the line saying
    what happens to each row it finds.
    """
    lines = describe_physical(planned.root).split("\n")
    if actual is not None and actual.plan is not None:
        measured = {op.operator_id: op for op in _walk(actual.plan)}
        lines = [
            _annotate(line, node, measured.get(node.node_id))
            for line, node in zip(lines, walk_physical(planned.root), strict=False)
        ]
    if prologue is not None:
        lines = [prologue, *(f"  {line}" for line in lines)]
    if epilogue is not None:
        lines.append(f"  {epilogue}")

    lines.append("")
    lines.append(
        f"Statistics: {planned.statistics.row_count} rows, "
        f"{planned.statistics.page_count} pages"
        + (
            " (STALE: the table has been written to since ANALYZE)"
            if planned.statistics_are_stale
            else ""
        )
    )
    if planned.rewrites:
        lines.append(f"Rewrites applied: {', '.join(planned.rewrites)}")
    if len(planned.alternatives) > 1:
        # Grouped by which question each was an answer to. A join has several
        # independent decisions, and a flat list of them reads as a
        # contradiction: three entries marked "chosen" for what looks like one
        # choice.
        for decision in dict.fromkeys(item.decision for item in planned.alternatives):
            considered = [
                item for item in planned.alternatives if item.decision == decision
            ]
            if (
                len(considered) == 1
                and considered[0].chosen
                and len(planned.alternatives) > 2
            ):
                lines.append(f"Decided {decision}: {considered[0].description}")
                continue
            lines.append(f"Considered {decision}:")
            for alternative in considered:
                marker = "->" if alternative.chosen else "  "
                reason = (
                    f"  [{alternative.rejected_because}]"
                    if alternative.rejected_because
                    else ""
                )
                lines.append(
                    f"  {marker} {alternative.description}  "
                    f"cost={alternative.cost.total:.1f} "
                    f"rows={alternative.cost.rows:.0f}{reason}"
                )
    return lines


def _annotate(line: str, node: PhysicalNode, measured: Operator | None) -> str:
    """Append actual figures beside the estimate, PostgreSQL-style."""
    if measured is None:
        return line
    return (
        f"{line} (actual rows={measured.stats.output_rows} "
        f"time={measured.stats.duration_ns / 1e6:.2f}ms)"
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


@dataclass(slots=True)
class _Located:
    """The rows a DELETE or UPDATE is about to change, and how they were found."""

    record_ids: tuple[RecordId, ...]
    rows: tuple[Row, ...]
    planned: PlannedQuery
    plan: Operator
    stats: ExecutionStats


def _locate_rows(
    bound: BoundUpdate | BoundDelete,
    database: Database,
    context: ExecutionContext,
    *,
    planned: PlannedQuery | None = None,
) -> _Located:
    """Run the row-locating plan to completion, *before* changing anything.

    Draining the scan first is not laziness deferred, it is required, and the
    reason has a name. Halloween::

        UPDATE salaries SET pay = pay * 1.1 WHERE pay < 50000

    If the scan is still open while the update runs, and the update writes a new
    version of each row, the scan reaches the new version too. It still matches
    (``pay`` is only 10% higher), so it is raised again, and again, until it
    escapes the predicate. The bug was found at IBM on Hallowe'en 1976 and the
    name stuck.

    ChenDB is exposed to it for a reason worth naming: an MVCC update *inserts*,
    and a transaction can always see its own writes, so the new version is
    genuinely reachable by the scan that produced the old one. Materialising the
    row set closes the loop. PostgreSQL relies on the fact that its scan will not
    return a tuple its own command already made; SQL Server inserts an explicit
    ``Eager Spool`` into the plan, which is this function with an operator around
    it.

    The cost is memory proportional to the rows matched. The ceiling on which
    is ``context.max_rows``, so a statement cannot buffer the whole table.
    """
    if planned is None:
        planned = plan_row_source(
            bound,
            database,
            tracer=context.tracer,
            options=context.planner_options or DEFAULT_PLANNER_OPTIONS,
        )
    plan = materialise(planned.root, database, context)

    record_ids: list[RecordId] = []
    rows: list[Row] = []
    stats = ExecutionStats()
    scan = _find_scan(plan)
    limit = context.max_rows if context.max_rows is not None else DEFAULT_MAX_ROWS

    try:
        context.controller.arm()
        plan.open()
        while (row := plan.next()) is not None:
            if scan is None or scan.last_record_id is None:  # pragma: no cover
                raise ExecutionError("row source produced a row with no address")
            record_ids.append(scan.last_record_id)
            rows.append(row)
            if len(record_ids) >= limit:
                # Silently changing the first 10,000 matches and reporting
                # success would be the worst possible outcome, so this raises.
                raise ExecutionError(
                    f"more than {limit} rows match; ChenDB will not change part "
                    f"of a statement's rows and call it done"
                )
    finally:
        plan.close()

    stats.rows_scanned = scan.stats.input_rows if scan else 0
    stats.rows_rejected = sum(
        operator.rows_rejected for operator in _walk(plan) if isinstance(operator, Filter)
    )
    return _Located(tuple(record_ids), tuple(rows), planned, plan, stats)


def _execute_delete(
    statement: DeleteStatement,
    database: Database,
    context: ExecutionContext,
    *,
    planned: PlannedQuery | None = None,
) -> QueryResult:
    bound = bind_delete(statement, database.catalog)
    located = _locate_rows(bound, database, context, planned=planned)
    deleted = database.delete_many(bound.table_name, located.record_ids)
    database.sync()

    stats = located.stats
    stats.rows_affected = deleted
    return QueryResult(
        statement_kind=statement.node_type,
        record_ids=located.record_ids,
        stats=stats,
        plan=located.plan,
        planned=located.planned,
        message=_mutation_message(
            "deleted", deleted, len(located.record_ids), bound.table_name, "from"
        ),
    )


def _execute_update(
    statement: UpdateStatement,
    database: Database,
    context: ExecutionContext,
    *,
    planned: PlannedQuery | None = None,
) -> QueryResult:
    bound = bind_update(statement, database.catalog)
    located = _locate_rows(bound, database, context, planned=planned)

    # Every right-hand side is evaluated against the row as it was, so
    # `SET a = b, b = a` swaps rather than assigning `a` to both.
    updates: list[tuple[RecordId, list[Any]]] = []
    for record_id, row in zip(located.record_ids, located.rows, strict=True):
        values = list(row)
        for assignment in bound.assignments:
            values[assignment.column_index] = evaluate(
                assignment.value,
                row,
                tracer=context.tracer,
                operator_id="update_1",
            )
        updates.append((record_id, values))

    replaced = database.update_many(bound.table_name, updates)
    database.sync()

    stats = located.stats
    stats.rows_affected = len(replaced)
    return QueryResult(
        statement_kind=statement.node_type,
        record_ids=tuple(replaced),
        stats=stats,
        plan=located.plan,
        planned=located.planned,
        message=_mutation_message(
            "updated", len(replaced), len(located.record_ids), bound.table_name, "in"
        ),
    )


def _mutation_message(
    verb: str, changed: int, matched: int, table: str, preposition: str
) -> str:
    """Report the change, and any gap between matched and changed.

    The gap is rows another session got to first. Reporting only the number
    changed would make a lost update look like a clean one.
    """
    message = f"{verb} {changed} row(s) {preposition} {table}"
    if changed != matched:
        message += (
            f"; {matched - changed} of the {matched} matched were changed by "
            f"another session first and were skipped"
        )
    return message


def _execute_select(
    statement: SelectStatement,
    database: Database,
    context: ExecutionContext,
    *,
    planned: PlannedQuery | None = None,
) -> QueryResult:
    statement = fold_subqueries(statement, database, context)
    bound = bind_select(statement, database.catalog)
    # EXPLAIN ANALYZE has already planned; re-planning would emit a second set
    # of planner events and re-gather statistics for no gain.
    if planned is None:
        planned = plan_query(
            bound,
            database,
            tracer=context.tracer,
            options=context.planner_options or DEFAULT_PLANNER_OPTIONS,
        )
    plan = materialise(planned.root, database, context)

    rows: list[Row] = []
    record_ids: list[RecordId] = []
    cancelled = False
    stats = ExecutionStats()
    scan = _find_scan(plan)
    limit = context.max_rows if context.max_rows is not None else DEFAULT_MAX_ROWS

    try:
        # Everything above this point (binding, statistics, costing) is not
        # steppable. Arming here is what makes "step" mean "advance the query"
        # rather than "advance the planner's scan of chendb_tables".
        context.controller.arm()
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
        operator.rows_rejected for operator in _walk(plan) if isinstance(operator, Filter)
    )

    return QueryResult(
        statement_kind=statement.node_type,
        columns=bound.output_columns,
        rows=tuple(rows),
        record_ids=tuple(record_ids),
        stats=stats,
        plan=plan,
        planned=planned,
        cancelled=cancelled,
    )


def execute_script(
    sql: str,
    database: Database,
    *,
    tracer: Tracer | None = None,
    controller: StepController | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    planner_options: PlannerOptions = DEFAULT_PLANNER_OPTIONS,
    atomic: bool = True,
) -> list[QueryResult]:
    """Parse and run every statement in ``sql``, in order, all or nothing.

    Since Milestone 8 a script that fails half-way leaves the database as it
    was. The whole script runs in one implicit transaction, committed when the
    last statement finishes and rolled back if any of them raises.

    That is deliberately **not** the SQL standard, which autocommits each
    statement. It is what this function's docstring promised from Milestone 3
    onward, and it is the more useful default for a script: half-applied setup
    is rarely what anyone wants. A client that needs per-statement autocommit
    sends one statement at a time, which is what the query API does not do and
    what ``atomic=False`` is for.

    A script that manages transactions itself still works, and takes ownership
    when it does. ``BEGIN`` adopts the implicit transaction rather than nesting,
    and from that point the script's own ``COMMIT`` ends it. A script of just
    ``BEGIN;`` leaves a transaction open, which is what PostgreSQL does with the
    same text in one simple-query message. A ``COMMIT`` part-way through ends
    the transaction, so the statements after it run in a fresh implicit one.

    A failure still rolls back, explicit or not. ChenDB has no "aborted but
    open" state to park a transaction in the way PostgreSQL does, and leaving a
    half-failed transaction open would strand a client that never sends
    ``ROLLBACK``.
    """
    statements = parse(sql, tracer=tracer)
    if not atomic:
        return [
            execute_statement(
                statement,
                database,
                tracer=tracer,
                controller=controller,
                max_rows=max_rows,
                planner_options=planner_options,
            )
            for statement in statements
        ]

    # This *session's* transaction, not the default session's: the two
    # differ the moment a client passes ``?session=``, and asking the wrong one
    # leaves an implicit transaction open that nothing ever commits.
    outer = database.active_transaction
    if outer is None:
        database.begin(implicit=True)
    results: list[QueryResult] = []
    try:
        for statement in statements:
            results.append(
                execute_statement(
                    statement,
                    database,
                    tracer=tracer,
                    controller=controller,
                    max_rows=max_rows,
                    planner_options=planner_options,
                )
            )
    except BaseException:
        # Only unwind what this call started. A caller that had its own
        # transaction open keeps it, and decides for itself.
        if outer is None and database.active_transaction is not None:
            database.rollback()
        raise

    # Auto-commit only what is still *ours*. A ``BEGIN`` in the script turned
    # the implicit transaction explicit, which means the client has taken
    # ownership and is going to send its own COMMIT: possibly in a later
    # request. Committing it here would make a lone ``BEGIN;`` a no-op.
    active = database.active_transaction
    if outer is None and active is not None and active.implicit:
        database.commit()
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


# --------------------------------------------------------------------------
# Subqueries (Milestone 23)
# --------------------------------------------------------------------------


def fold_subqueries(
    statement: SelectStatement, database: Database, context: ExecutionContext
) -> SelectStatement:
    """Run every ``(SELECT …)`` once and substitute the value it produced.

    An uncorrelated subquery names nothing outside itself, so it depends on no
    row of the query around it and has **one value for the whole statement**.
    Running it once and folding the result into a literal is therefore not an
    optimisation of some more general mechanism; for this shape it is the whole
    semantics, and it happens before binding so everything downstream (the
    planner, the index matcher, the cost model) sees an ordinary constant and
    needs to know nothing about subqueries at all.

    ``WHERE total > (SELECT AVG(total) FROM orders)`` becomes
    ``WHERE total > 812``, which an index on ``total`` can answer.

    A **correlated** subquery is refused by name. It is a different feature with
    a different implementation, either a join or one execution per outer row,
    and running one per row without saying so would turn a query somebody wrote
    into a plan nobody would have chosen.
    """
    outer = {reference.binding_name.casefold() for reference in statement.tables}
    return _map_expressions(statement, lambda item: _fold(item, database, context, outer))


def _fold(
    expression: Expression,
    database: Database,
    context: ExecutionContext,
    outer: set[str],
) -> Expression:
    """Replace every subquery in one expression tree with its value."""
    match expression:
        case ScalarSubquery(statement=inner):
            _refuse_if_correlated(expression, inner, outer)
            value = _run_scalar(expression, inner, database, context)
            return Literal(
                node_id=expression.node_id,
                span=expression.span,
                value=value,
                data_type=_literal_type(value),
            )
        case UnaryOp(operand=operand):
            folded = _fold(operand, database, context, outer)
            if folded is operand:
                return expression
            return replace(expression, operand=folded)
        case BinaryOp(left=left, right=right):
            new_left = _fold(left, database, context, outer)
            new_right = _fold(right, database, context, outer)
            if new_left is left and new_right is right:
                return expression
            return replace(expression, left=new_left, right=new_right)
        case IsNullTest(operand=operand):
            folded = _fold(operand, database, context, outer)
            return expression if folded is operand else replace(expression, operand=folded)
        case FunctionCall(argument=argument) if argument is not None:
            folded = _fold(argument, database, context, outer)
            return (
                expression if folded is argument else replace(expression, argument=folded)
            )
    return expression


def _run_scalar(
    node: ScalarSubquery,
    inner: SelectStatement,
    database: Database,
    context: ExecutionContext,
) -> Any:
    """Execute the subquery and reduce its result to one value.

    Two errors, and both are PostgreSQL's. One column, because a value is one
    value; at most one row, because more than one has no defensible answer and
    picking the first would make the query depend on physical order. Zero rows
    is **NULL**, not an error, which is what makes
    ``WHERE x = (SELECT … WHERE false)`` return nothing rather than fail.
    """
    if len(inner.projections) != 1 or isinstance(inner.projections[0].expression, Star):
        raise _subquery_error(node, "a subquery used as a value must return one column")

    result = _execute_select(inner, database, context)
    if not result.rows:
        return None
    if len(result.rows) > 1:
        raise _subquery_error(
            node,
            f"a subquery used as a value returned {len(result.rows)} rows; "
            f"add a LIMIT, an aggregate, or a condition that makes it one",
        )
    return result.rows[0][0]


def _refuse_if_correlated(
    node: ScalarSubquery, inner: SelectStatement, outer: set[str]
) -> None:
    """Refuse a subquery that reaches into the query around it.

    Detected by name rather than by letting the binder fail: an unqualified
    column that does not exist inside is a typo and deserves "no column named",
    while ``o.total`` inside a subquery over ``items`` is a correlated
    reference and deserves to be told so.
    """
    inside = {reference.binding_name.casefold() for reference in inner.tables}
    for child in walk(inner):
        if not isinstance(child, ColumnRef) or child.table is None:
            continue
        qualifier = child.table.casefold()
        if qualifier in outer and qualifier not in inside:
            raise UnsupportedSqlError(
                f"a correlated subquery is not implemented yet: "
                f"{child.qualified_name!r} belongs to the query outside this one",
                start=child.span.start,
                end=child.span.end,
                line=child.span.line,
                column=child.span.column,
                found=child.qualified_name,
            )


def _subquery_error(node: ScalarSubquery, message: str) -> BindingError:
    return BindingError(
        message,
        start=node.span.start,
        end=node.span.end,
        line=node.span.line,
        column=node.span.column,
    )


def _literal_type(value: Any) -> DataType | None:
    match value:
        case bool():
            return DataType.BOOLEAN
        case int():
            return DataType.INTEGER
        case float():
            return DataType.FLOAT
        case str():
            return DataType.TEXT
    return None


def _map_expressions(
    statement: SelectStatement, transform: Callable[[Expression], Expression]
) -> SelectStatement:
    """Apply ``transform`` to every expression a ``SELECT`` can hold.

    Written out rather than derived from the dataclass fields, because a generic
    rewriter would have to know which tuples hold nodes that hold expressions
    and which hold expressions directly, and the list is six entries long.
    """
    return replace(
        statement,
        projections=tuple(
            replace(item, expression=transform(item.expression))
            for item in statement.projections
        ),
        joins=tuple(replace(join, on=transform(join.on)) for join in statement.joins),
        where=transform(statement.where) if statement.where is not None else None,
        group_by=tuple(transform(key) for key in statement.group_by),
        having=transform(statement.having) if statement.having is not None else None,
        order_by=tuple(
            replace(item, expression=transform(item.expression))
            for item in statement.order_by
        ),
    )
