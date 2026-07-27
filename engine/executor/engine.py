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
    IndexScan,
    Operator,
    Project,
    ScanOperator,
    SeqScan,
)
from engine.parser.ast import (
    AnalyzeStatement,
    BeginStatement,
    CommitStatement,
    CreateIndexStatement,
    CreateTableStatement,
    ExplainStatement,
    InsertStatement,
    RollbackStatement,
    SelectStatement,
    Statement,
)
from engine.parser.parser import parse
from engine.planner.logical import (
    LogicalFilter,
    LogicalNode,
    LogicalProject,
    LogicalScan,
    walk_logical,
)
from engine.planner.physical import (
    DEFAULT_PLANNER_OPTIONS,
    PhysicalFilter,
    PhysicalIndexScan,
    PhysicalNode,
    PhysicalProject,
    PhysicalSeqScan,
    PlannedQuery,
    PlannerOptions,
    describe_physical,
    plan_select,
    walk_physical,
)
from engine.serialization.record import Row
from engine.storage.heap import RecordId
from engine.transaction.manager import TransactionState

if TYPE_CHECKING:
    from engine.database import Database

__all__ = [
    "ExecutionStats",
    "QueryResult",
    "build_logical_plan",
    "build_select_plan",
    "execute_script",
    "execute_statement",
    "materialise",
    "plan_query",
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

    Straight structural translation with no decisions in it — every decision
    belongs to a rewrite rule or to the cost model, where it can be named and
    inspected. Milestone 3 dropped an identity projection here; that is now
    :mod:`engine.optimizer.rules`.
    """
    plan: LogicalNode = LogicalScan("scan", bound.table_name, bound.input_schema)
    if bound.where is not None:
        plan = LogicalFilter("filter", bound.where, plan)
    return LogicalProject("project", bound.projections, bound.output_columns, plan)


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
    without opening an index or a heap — which is what lets ``EXPLAIN`` cost a
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
#: every client that can already display a SELECT can display an EXPLAIN —
#: which is exactly why PostgreSQL's EXPLAIN returns a one-column result set.
_EXPLAIN_COLUMNS: tuple[ResultColumn, ...] = (ResultColumn("QUERY PLAN", None),)


def _execute_explain(
    statement: ExplainStatement,
    database: Database,
    context: ExecutionContext,
    max_rows: int,
) -> QueryResult:
    """Plan the inner statement, optionally run it, and return the plan as rows."""
    inner = statement.statement
    if not isinstance(inner, SelectStatement):
        raise BindingError(
            f"EXPLAIN can only explain a SELECT, not {inner.node_type}; "
            f"nothing else has an operator tree",
            start=inner.span.start,
            end=inner.span.end,
            line=inner.span.line,
            column=inner.span.column,
        )

    bound = bind_select(inner, database.catalog)
    planned = plan_query(
        bound,
        database,
        tracer=context.tracer,
        options=context.planner_options or DEFAULT_PLANNER_OPTIONS,
    )

    actual: QueryResult | None = None
    if statement.analyze:
        # EXPLAIN ANALYZE runs the query. The row counts it reports are the real
        # ones, which is the only way to see where an estimate went wrong.
        actual = _execute_select(inner, database, context, planned=planned)

    lines = _explain_lines(planned, actual)
    return QueryResult(
        statement_kind=statement.node_type,
        columns=_EXPLAIN_COLUMNS,
        rows=tuple((line,) for line in lines[:max_rows]),
        plan=actual.plan if actual else None,
        planned=planned,
        stats=actual.stats if actual else ExecutionStats(rows_returned=len(lines)),
    )


def _explain_lines(planned: PlannedQuery, actual: QueryResult | None) -> list[str]:
    """The plan as text, in the order PostgreSQL prints it."""
    lines = describe_physical(planned.root).split("\n")
    if actual is not None and actual.plan is not None:
        measured = {op.operator_id: op for op in _walk(actual.plan)}
        lines = [
            _annotate(line, node, measured.get(node.node_id))
            for line, node in zip(lines, walk_physical(planned.root), strict=False)
        ]

    lines.append("")
    lines.append(
        f"Statistics: {planned.statistics.row_count} rows, "
        f"{planned.statistics.page_count} pages"
        + (
            " (STALE — the table has been written to since ANALYZE)"
            if planned.statistics_are_stale
            else ""
        )
    )
    if planned.rewrites:
        lines.append(f"Rewrites applied: {', '.join(planned.rewrites)}")
    if len(planned.alternatives) > 1:
        lines.append("Alternatives considered:")
        for alternative in planned.alternatives:
            marker = "->" if alternative.chosen else "  "
            reason = (
                f"  [{alternative.rejected_because}]"
                if alternative.rejected_because
                else ""
            )
            lines.append(
                f"  {marker} {alternative.description}  "
                f"cost={alternative.cost.total:.1f} rows={alternative.cost.rows:.0f}{reason}"
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


def _execute_select(
    statement: SelectStatement,
    database: Database,
    context: ExecutionContext,
    *,
    planned: PlannedQuery | None = None,
) -> QueryResult:
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
        # Everything above this point — binding, statistics, costing — is not
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
    """Parse and run every statement in ``sql``, in order — all or nothing.

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
    and from that point the script's own ``COMMIT`` ends it — a script of just
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

    outer = database.transactions.active
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
        if outer is None and database.transactions.active is not None:
            database.rollback()
        raise

    # Auto-commit only what is still *ours*. A ``BEGIN`` in the script turned
    # the implicit transaction explicit, which means the client has taken
    # ownership and is going to send its own COMMIT — possibly in a later
    # request. Committing it here would make a lone ``BEGIN;`` a no-op.
    active = database.transactions.active
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
