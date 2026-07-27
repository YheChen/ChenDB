"""The engine-to-API boundary.

Every conversion from an engine dataclass into a wire model happens in this
module and nowhere else.  That is the rule
``tests/unit/test_architecture_boundaries.py`` enforces, and it buys three
things:

* the engine never imports Pydantic, so it stays embeddable and dependency-free;
* changing the on-disk layout does not silently change the public API;
* there is exactly one place to look when a field renders wrong.

Mappers take engine values and return API models.  They are pure functions with
no I/O, so they can — and must — run *outside* any engine lock.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from engine.catalog.catalog import CatalogStats, IndexInfo, TableInfo
from engine.database import Database
from engine.diagnostics import SinkStats, TraceLevel, TraceRecord
from engine.errors import SqlError
from engine.executor.binder import ResultColumn
from engine.executor.engine import QueryResult
from engine.executor.operators import Operator
from engine.index.bplustree import IndexStats, TreeSnapshot
from engine.parser.analyze import ParseOutcome
from engine.parser.ast import Node, walk
from engine.parser.tokens import Token
from engine.planner.physical import (
    Alternative,
    PhysicalNode,
    PlannedQuery,
    walk_physical,
)
from engine.serialization.record import FieldLayout, RecordLayout
from engine.serialization.schema import Column, Schema
from engine.server.executions import Execution
from engine.server.schemas.buffer import (
    BufferPoolResponse,
    BufferPoolStatsModel,
    FrameModel,
)
from engine.server.schemas.catalog import (
    CatalogStatsModel,
    TableDetail,
    TableStorageModel,
    TableSummary,
)
from engine.server.schemas.concurrency import (
    LockEntryModel,
    LockStatsModel,
    LockTableResponse,
    SessionListResponse,
    SessionModel,
    WaitForEdge,
)
from engine.server.schemas.database import (
    ColumnModel,
    DatabaseDetail,
    PagerStatsModel,
    RecordIdModel,
    RowModel,
    SchemaModel,
)
from engine.server.schemas.events import TraceRecordModel, TraceStatsModel
from engine.server.schemas.indexes import (
    IndexDetail,
    IndexStatsModel,
    IndexSummary,
    TreeNodeModel,
    TreeSnapshotModel,
)
from engine.server.schemas.pages import (
    FieldLayoutModel,
    HeaderFieldModel,
    PageDetailModel,
    PageSummaryModel,
    RecordLayoutModel,
    SlotDetailModel,
)
from engine.server.schemas.query import (
    ExecutionDetail,
    ExecutionSummary,
    OperatorNodeModel,
    PlanAlternativeModel,
    PlanModel,
    PlanStatisticsModel,
    QueryResultModel,
    ResultColumnModel,
)
from engine.server.schemas.sql import (
    AstNodeModel,
    AstTreeModel,
    ParseResponse,
    SqlErrorModel,
    StatementModel,
    TokenModel,
)
from engine.server.schemas.transactions import (
    TransactionListResponse,
    TransactionModel,
    UndoRecordModel,
)
from engine.server.schemas.wal import (
    RecoveryReportModel,
    WalRecordModel,
    WalResponse,
    WalStatsModel,
)
from engine.storage.buffer import PoolSnapshot
from engine.storage.constants import INVALID_PAGE_ID
from engine.storage.heap import RecordId
from engine.storage.inspect import HeaderField, PageDetail, PageSummary, SlotDetail
from engine.storage.pager import PagerStats
from engine.transaction.manager import Transaction, TransactionManager
from engine.wal.log import WriteAheadLog
from engine.wal.recovery import RecoveryReport

__all__ = [
    "ast_node_to_api",
    "column_to_api",
    "database_detail_to_api",
    "page_detail_to_api",
    "page_summary_to_api",
    "pager_stats_to_api",
    "parse_outcome_to_api",
    "record_id_to_api",
    "row_to_api",
    "schema_to_api",
    "sql_error_to_api",
    "token_to_api",
    "trace_record_to_api",
    "trace_stats_to_api",
]


def _optional_page_id(page_id: int) -> int | None:
    """Render the on-disk null sentinel as JSON ``null``."""
    return None if page_id == INVALID_PAGE_ID else page_id


# -- schema ----------------------------------------------------------------


def column_to_api(column: Column) -> ColumnModel:
    return ColumnModel(
        name=column.name,
        type=column.data_type.sql_name,  # type: ignore[arg-type]
        nullable=column.nullable,
        primary_key=column.primary_key,
        fixed_size=column.fixed_size,
    )


def schema_to_api(schema: Schema) -> SchemaModel:
    return SchemaModel(
        columns=[column_to_api(column) for column in schema],
        null_bitmap_size=schema.null_bitmap_size,
        fixed_row_size=schema.fixed_row_size,
    )


# -- rows ------------------------------------------------------------------


def record_id_to_api(record_id: RecordId) -> RecordIdModel:
    return RecordIdModel(page_id=record_id.page_id, slot_id=record_id.slot_id)


def row_to_api(record_id: RecordId, values: Sequence[Any]) -> RowModel:
    return RowModel(record_id=record_id_to_api(record_id), values=list(values))


# -- statistics ------------------------------------------------------------


def pager_stats_to_api(stats: PagerStats) -> PagerStatsModel:
    return PagerStatsModel(**stats.as_dict())


# -- database --------------------------------------------------------------


def database_detail_to_api(
    db: Database,
    *,
    tables: Sequence[TableInfo],
    trace_level: TraceLevel,
    file_size_bytes: int,
) -> DatabaseDetail:
    meta = db.pager.meta
    return DatabaseDetail(
        database_id=db.database_id,
        page_size=db.page_size,
        page_count=db.page_count,
        file_size_bytes=file_size_bytes,
        format_version=meta.format_version,
        table_names=[info.name for info in tables],
        table_count=len(tables),
        free_list_head=_optional_page_id(meta.free_list_head),
        stats=pager_stats_to_api(db.stats),
        trace_level=trace_level.name,
    )


# -- pages -----------------------------------------------------------------


def header_field_to_api(field: HeaderField) -> HeaderFieldModel:
    return HeaderFieldModel(
        name=field.name,
        offset=field.offset,
        size=field.size,
        value=field.value,
        raw_hex=field.raw_hex,
        description=field.description,
    )


def page_summary_to_api(summary: PageSummary) -> PageSummaryModel:
    return PageSummaryModel(
        page_id=summary.page_id,
        page_type=summary.page_type,
        file_offset=summary.file_offset,
        lsn=summary.lsn,
        checksum=summary.checksum,
        checksum_valid=summary.checksum_valid,
        slot_count=summary.slot_count,
        live_record_count=summary.live_record_count,
        free_space=summary.free_space,
        reclaimable_space=summary.reclaimable_space,
        next_page_id=summary.next_page_id,
        owner=summary.owner,
        error=summary.error,
        # No buffer pool yet, so nothing is ever cached-and-dirty.
        dirty=False,
    )


def field_layout_to_api(field: FieldLayout) -> FieldLayoutModel:
    return FieldLayoutModel(
        index=field.index,
        name=field.name,
        type_name=field.type_name,
        is_null=field.is_null,
        offset=field.offset,
        length=field.length,
        value=field.value,
    )


def _bitmap_bits(bitmap: bytes, column_count: int) -> list[bool]:
    """Expand the packed null bitmap into one boolean per column."""
    return [bool(bitmap[index // 8] >> (index % 8) & 1) for index in range(column_count)]


def record_layout_to_api(layout: RecordLayout) -> RecordLayoutModel:
    return RecordLayoutModel(
        values=list(layout.values),
        fields=[field_layout_to_api(field) for field in layout.fields],
        null_bitmap_hex=layout.null_bitmap.hex(),
        null_bitmap_bits=_bitmap_bits(layout.null_bitmap, len(layout.fields)),
        null_bitmap_size=layout.null_bitmap_size,
        total_size=layout.total_size,
    )


def slot_detail_to_api(slot: SlotDetail) -> SlotDetailModel:
    return SlotDetailModel(
        slot_id=slot.slot_id,
        offset=slot.offset,
        length=slot.length,
        is_live=slot.is_live,
        raw_hex=slot.raw_hex,
        record=record_layout_to_api(slot.record) if slot.record else None,
        decode_error=slot.decode_error,
        xmin=slot.xmin,
        xmax=slot.xmax,
    )


def page_detail_to_api(detail: PageDetail) -> PageDetailModel:
    return PageDetailModel(
        summary=page_summary_to_api(detail.summary),
        header_fields=[header_field_to_api(f) for f in detail.header_fields],
        slots=[slot_detail_to_api(slot) for slot in detail.slots],
        raw_hex=detail.raw.hex(),
        page_size=detail.page_size,
        header_size=detail.header_size,
        slot_directory_end=detail.slot_directory_end,
        free_start=detail.free_start,
        free_end=detail.free_end,
    )


# -- diagnostics -----------------------------------------------------------


def _event_payload(event: object) -> dict[str, Any]:
    """Flatten an event dataclass into JSON-safe fields.

    ``dataclasses.asdict`` would recurse and deep-copy; events are flat, so a
    shallow field walk is both correct and cheaper. Non-primitive values are
    stringified rather than dropped, so a future event carrying a richer type
    still renders instead of vanishing.
    """
    payload: dict[str, Any] = {}
    for field in dataclasses.fields(event):  # type: ignore[arg-type]
        value = getattr(event, field.name)
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[field.name] = value
        else:
            payload[field.name] = str(value)
    return payload


def trace_record_to_api(item: TraceRecord) -> TraceRecordModel:
    return TraceRecordModel(
        seq=item.seq,
        timestamp_ns=item.timestamp_ns,
        category=item.category.value,  # type: ignore[arg-type]
        level=item.level.name,  # type: ignore[arg-type]
        event_type=item.event_type,
        event=_event_payload(item.event),
    )


def trace_stats_to_api(stats: SinkStats, level: TraceLevel) -> TraceStatsModel:
    return TraceStatsModel(
        capacity=stats.capacity,
        size=stats.size,
        total_recorded=stats.total_recorded,
        dropped=stats.dropped,
        level=level.name,  # type: ignore[arg-type]
    )


# -- SQL front end (Milestone 2) -------------------------------------------


def token_to_api(index: int, token: Token) -> TokenModel:
    return TokenModel(
        index=index,
        type=token.type.value,
        lexeme=token.lexeme,
        start=token.span.start,
        end=token.span.end,
        line=token.span.line,
        column=token.span.column,
        keyword=token.keyword.value if token.keyword else None,
        value=token.value,
    )


def _node_label(node: Node) -> str:
    """A short label for a tree row.

    Falls back to the node type when there is nothing shorter to show, so a row
    is never blank.
    """
    attributes = node.attributes()
    for key in ("operator", "name", "value", "alias", "kind"):
        value = attributes.get(key)
        if value is not None and value != "":
            return str(value)
    if "negated" in attributes:
        return "IS NOT NULL" if attributes["negated"] else "IS NULL"
    return ""


def ast_node_to_api(node: Node, sql: str) -> AstNodeModel:
    return AstNodeModel(
        node_id=node.node_id,
        node_type=node.node_type,
        start=node.span.start,
        end=node.span.end,
        line=node.span.line,
        column=node.span.column,
        text=node.text_in(sql),
        children=[child.node_id for child in node.children()],
        attributes=node.attributes(),
        label=_node_label(node),
    )


def sql_error_to_api(error: SqlError) -> SqlErrorModel:
    expected = getattr(error, "expected", ())
    return SqlErrorModel(
        kind=type(error).__name__,
        message=error.message,
        start=error.start,
        end=max(error.end, error.start + 1),
        line=error.line,
        column=error.column,
        expected=list(expected),
        found=getattr(error, "found", ""),
    )


def parse_outcome_to_api(outcome: ParseOutcome) -> ParseResponse:
    """Flatten the whole outcome into one response.

    Node ids are unique across the script because the parser numbers them from a
    single counter, so several statements can share one flat node list.
    """
    nodes: list[AstNodeModel] = []
    for statement in outcome.statements:
        nodes.extend(ast_node_to_api(node, outcome.sql) for node in walk(statement))
    nodes.sort(key=lambda node: node.node_id)

    return ParseResponse(
        sql=outcome.sql,
        ok=outcome.ok,
        tokens=[token_to_api(index, token) for index, token in enumerate(outcome.tokens)],
        ast=AstTreeModel(
            nodes=nodes,
            root_ids=[statement.node_id for statement in outcome.statements],
        ),
        statements=[
            StatementModel(
                root_id=statement.node_id,
                kind=statement.node_type,
                start=statement.span.start,
                end=statement.span.end,
                text=statement.text_in(outcome.sql),
            )
            for statement in outcome.statements
        ],
        error=sql_error_to_api(outcome.error) if outcome.error else None,
        lexed_ok=outcome.lexed_ok,
        token_count=len(outcome.tokens),
        node_count=outcome.node_count,
        duration_ns=outcome.duration_ns,
    )


# -- query execution (Milestone 3) -----------------------------------------


def result_column_to_api(column: ResultColumn) -> ResultColumnModel:
    return ResultColumnModel(
        name=column.name,
        type=column.data_type.sql_name if column.data_type else None,
    )


def _operator_nodes(
    root: Operator, estimates: dict[str, PhysicalNode]
) -> list[OperatorNodeModel]:
    """Flatten an operator tree, parents before children, pairing estimates.

    Matched by ``operator_id``, which the planner assigns and the executor
    carries through unchanged. That shared id is the only thing linking the two
    trees, and it is what lets the UI put estimated and actual on one line.
    """
    nodes: list[OperatorNodeModel] = []
    stack = [root]
    while stack:
        operator = stack.pop()
        planned = estimates.get(operator.operator_id)
        nodes.append(
            OperatorNodeModel(
                operator_id=operator.operator_id,
                operator_type=operator.operator_type,
                detail=operator.detail,
                children=[child.operator_id for child in operator.children],
                output_columns=[
                    result_column_to_api(column) for column in operator.output_columns
                ],
                estimated_rows=round(planned.estimated.rows, 1) if planned else None,
                estimated_cost=round(planned.total_cost, 2) if planned else None,
                estimated_io_cost=round(planned.estimated.io, 2) if planned else None,
                estimated_cpu_cost=round(planned.estimated.cpu, 2) if planned else None,
                next_calls=operator.stats.next_calls,
                input_rows=operator.stats.input_rows,
                output_rows=operator.stats.output_rows,
                rows_rejected=getattr(operator, "rows_rejected", 0),
                duration_ns=operator.stats.duration_ns,
            )
        )
        stack.extend(reversed(operator.children))
    return nodes


def plan_alternative_to_api(alternative: Alternative) -> PlanAlternativeModel:
    return PlanAlternativeModel(
        description=alternative.description,
        access_path=alternative.access_path,
        estimated_cost=round(alternative.cost.total, 2),
        estimated_rows=round(alternative.cost.rows, 1),
        chosen=alternative.chosen,
        rejected_because=alternative.rejected_because,
        index_name=alternative.index_name,
    )


def plan_statistics_to_api(planned: PlannedQuery) -> PlanStatisticsModel:
    stats = planned.statistics
    return PlanStatisticsModel(
        table_name=stats.table_name,
        row_count=stats.row_count,
        page_count=stats.page_count,
        stale=planned.statistics_are_stale,
        gathered_at_ns=stats.gathered_at_ns,
    )


def plan_to_api(root: Operator, planned: PlannedQuery | None = None) -> PlanModel:
    """The running plan, annotated with what the planner expected of it."""
    estimates = (
        {node.node_id: node for node in walk_physical(planned.root)} if planned else {}
    )
    return PlanModel(
        nodes=_operator_nodes(root, estimates),
        root_id=root.operator_id,
        alternatives=[
            plan_alternative_to_api(alternative)
            for alternative in (planned.alternatives if planned else ())
        ],
        rewrites=list(planned.rewrites) if planned else [],
        estimated_cost=round(planned.estimated_cost, 2) if planned else None,
        statistics=plan_statistics_to_api(planned) if planned else None,
    )


def _json_row(row: Sequence[Any]) -> list[Any]:
    """Rows are already JSON-safe: every column type maps to a JSON scalar."""
    return list(row)


def query_result_to_api(result: QueryResult) -> QueryResultModel:
    stats = result.stats
    return QueryResultModel(
        statement_kind=result.statement_kind,
        returns_rows=result.returns_rows,
        message=result.message,
        columns=[result_column_to_api(column) for column in result.columns],
        rows=[_json_row(row) for row in result.rows],
        record_ids=[record_id_to_api(rid) for rid in result.record_ids],
        plan=plan_to_api(result.plan, result.planned) if result.plan is not None else None,
        rows_returned=stats.rows_returned,
        rows_affected=stats.rows_affected,
        rows_scanned=stats.rows_scanned,
        rows_rejected=stats.rows_rejected,
        pages_read=stats.pages_read,
        pages_written=stats.pages_written,
        duration_ns=stats.duration_ns,
        truncated=stats.truncated,
        cancelled=result.cancelled,
    )


def execution_summary_to_api(execution: Execution) -> ExecutionSummary:
    return ExecutionSummary(
        execution_id=execution.execution_id,
        database_id=execution.database_id,
        statement_kind=execution.statement_kind,
        state=execution.state.value,  # type: ignore[arg-type]
        steps_taken=execution.controller.steps_taken,
        age_seconds=round(execution.age_seconds, 3),
        idle_seconds=round(execution.idle_seconds, 3),
    )


def execution_detail_to_api(execution: Execution) -> ExecutionDetail:
    """Snapshot a stepped execution.

    Reads the controller's state once and the plan once, so the response cannot
    describe a pause reason from one instant and a plan from another.
    """
    reason = execution.pause_reason
    result = execution.result
    plan = result.plan if result is not None else None

    return ExecutionDetail(
        execution_id=execution.execution_id,
        database_id=execution.database_id,
        sql=execution.sql,
        statement_kind=execution.statement_kind,
        state=execution.state.value,  # type: ignore[arg-type]
        steps_taken=execution.controller.steps_taken,
        pause_kind=reason.kind.value if reason else None,
        pause_operator_id=reason.operator_id if reason else None,
        pause_detail=reason.detail if reason else "",
        plan=plan_to_api(plan, result.planned if result else None)
        if plan is not None
        else None,
        current_row=None,
        rows_so_far=len(result.rows) if result is not None else 0,
        result=query_result_to_api(result) if result is not None else None,
        error=execution.error,
        age_seconds=round(execution.age_seconds, 3),
        idle_seconds=round(execution.idle_seconds, 3),
    )


# -- catalog (Milestone 4) -------------------------------------------------


def table_summary_to_api(
    info: TableInfo, *, row_count: int, page_count: int
) -> TableSummary:
    return TableSummary(
        table_id=info.table_id,
        name=info.name,
        column_count=info.column_count,
        row_count=row_count,
        page_count=page_count,
        is_system=info.is_system,
    )


def table_storage_to_api(
    info: TableInfo,
    *,
    page_ids: Sequence[int],
    row_count: int,
    page_size: int,
    free_space: int,
    reclaimable_space: int,
) -> TableStorageModel:
    return TableStorageModel(
        first_page=info.first_page,
        last_page=info.last_page,
        page_ids=list(page_ids),
        page_count=len(page_ids),
        row_count=row_count,
        bytes_allocated=len(page_ids) * page_size,
        free_space=free_space,
        reclaimable_space=reclaimable_space,
    )


def table_detail_to_api(info: TableInfo, storage: TableStorageModel) -> TableDetail:
    return TableDetail(
        table_id=info.table_id,
        name=info.name,
        is_system=info.is_system,
        schema=schema_to_api(info.schema),
        columns=[column_to_api(column) for column in info.schema],
        storage=storage,
    )


def catalog_stats_to_api(stats: CatalogStats) -> CatalogStatsModel:
    return CatalogStatsModel(
        lookups=stats.lookups,
        cache_hits=stats.cache_hits,
        hit_rate=round(stats.hit_rate, 4),
        scans=stats.scans,
        tables_created=stats.tables_created,
        indexes_created=stats.indexes_created,
    )


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------


def index_summary_to_api(
    info: IndexInfo, *, height: int, entry_count: int, page_count: int
) -> IndexSummary:
    """The counts are passed in because computing them costs page reads.

    Keeping the I/O in the router means every mapper stays a pure function that
    can run outside the engine lock — the rule the whole module is built on.
    """
    return IndexSummary(
        index_id=info.index_id,
        name=info.name,
        table_name=info.table_name,
        column_name=info.column_name,
        column_position=info.column_position,
        data_type=info.data_type.sql_name,
        unique=info.unique,
        root_page=info.root_page,
        height=height,
        entry_count=entry_count,
        page_count=page_count,
    )


def tree_snapshot_to_api(snapshot: TreeSnapshot) -> TreeSnapshotModel:
    return TreeSnapshotModel(
        root_page_id=snapshot.root_page_id,
        height=snapshot.height,
        nodes=[
            TreeNodeModel(
                page_id=node.page_id,
                level=node.level,
                is_leaf=node.is_leaf,
                keys=list(node.keys),
                children=list(node.children),
                record_ids=list(node.record_ids),
                next_leaf_id=node.next_leaf_id,
                free_bytes=node.free_bytes,
                entry_count=node.entry_count,
            )
            for node in snapshot.nodes
        ],
        truncated=snapshot.truncated,
    )


def index_stats_to_api(stats: IndexStats) -> IndexStatsModel:
    return IndexStatsModel(**stats.as_dict())


def index_detail_to_api(
    info: IndexInfo, snapshot: TreeSnapshot, stats: IndexStats, *, entry_count: int
) -> IndexDetail:
    return IndexDetail(
        index=index_summary_to_api(
            info,
            height=snapshot.height,
            entry_count=entry_count,
            page_count=len(snapshot.nodes),
        ),
        tree=tree_snapshot_to_api(snapshot),
        stats=index_stats_to_api(stats),
    )


# --------------------------------------------------------------------------
# Buffer pool (Milestone 7)
# --------------------------------------------------------------------------


def buffer_pool_to_api(snapshot: PoolSnapshot, pager: PagerStats) -> BufferPoolResponse:
    """The frame grid plus the counters, for the pool view.

    Takes a frozen snapshot rather than the live pool, so this runs after the
    engine lock is released — the same rule every diagnostics mapper follows.
    """
    return BufferPoolResponse(
        capacity=snapshot.capacity,
        page_size=snapshot.page_size,
        resident=snapshot.resident,
        dirty=snapshot.dirty,
        bytes_used=snapshot.resident * snapshot.page_size,
        frames=[
            FrameModel(
                frame_id=frame.frame_id,
                page_id=frame.page_id,
                dirty=frame.dirty,
                reads=frame.reads,
                writes=frame.writes,
                recency=frame.recency,
                resident_for_ns=frame.resident_for_ns,
            )
            for frame in snapshot.frames
        ],
        stats=BufferPoolStatsModel(
            hits=snapshot.stats.hits,
            misses=snapshot.stats.misses,
            lookups=snapshot.stats.lookups,
            hit_rate=round(snapshot.stats.hit_rate, 4),
            evictions=snapshot.stats.evictions,
            dirty_evictions=snapshot.stats.dirty_evictions,
            writes_absorbed=snapshot.stats.writes_absorbed,
            flushes=snapshot.stats.flushes,
            pages_flushed=snapshot.stats.pages_flushed,
        ),
        logical_reads=pager.page_reads,
        physical_reads=pager.physical_reads,
        logical_writes=pager.page_writes,
        physical_writes=pager.physical_writes,
    )


def transaction_to_api(
    transaction: Transaction, *, with_records: bool = False
) -> TransactionModel:
    """One transaction.

    ``with_records`` is off by default because a finished transaction has no
    records to give — the undo log is released the moment it commits or aborts —
    and because an active one can hold thousands of them. The list view asks for
    them on the active transaction only.
    """
    records = (
        [
            UndoRecordModel(
                sequence=record.sequence,
                page_id=record.page_id,
                before_image_size=record.size,
                reason=record.reason,
            )
            for record in transaction.records()
        ]
        if with_records
        else []
    )
    return TransactionModel(
        transaction_id=transaction.transaction_id,
        state=transaction.state.value,  # type: ignore[arg-type]
        implicit=transaction.implicit,
        statements=transaction.statements,
        pages_written=transaction.pages_written,
        pages_held=transaction.pages_held,
        pages_restored=transaction.pages_restored,
        undo_bytes=transaction.undo_bytes,
        duration_ns=transaction.duration_ns,
        records=records,
    )


def transactions_to_api(
    manager: TransactionManager, session: str = "default"
) -> TransactionListResponse:
    """The timeline: what is open, and what has finished.

    Read from a manager rather than a snapshot, unlike the buffer pool mapper.
    That is safe for a different reason: the caller holds the engine lock while
    building the tuples, and every value copied out is an int, a bool or a
    frozen dataclass. There is no live object here to observe mid-mutation.
    """
    active = manager.active_in(session)
    return TransactionListResponse(
        active=(
            transaction_to_api(active, with_records=True) if active is not None else None
        ),
        history=[transaction_to_api(item) for item in manager.history()],
        history_limit=manager.HISTORY_LIMIT,
        in_transaction=active is not None,
        is_failed=manager.is_failed_in(session),
        in_explicit_transaction=active is not None and not active.implicit,
        undo_bytes=active.undo_bytes if active is not None else 0,
    )


def wal_record_to_api(record) -> WalRecordModel:
    return WalRecordModel(
        lsn=record.lsn,
        prev_lsn=record.prev_lsn,
        transaction_id=record.transaction_id,
        record_type=record.record_type.name.lower(),
        page_id=record.page_id,
        size=record.size,
        before_image_size=len(record.before_image),
        after_image_size=len(record.after_image),
    )


def wal_to_api(
    log: WriteAheadLog | None, *, limit: int, size_bytes: int
) -> WalResponse:
    """The log as a table, newest last.

    Page images are dropped here and nowhere else — that is the point of having
    a mapper. A record can carry two whole pages, so a thousand of them is
    megabytes of base64 that no panel renders; the *sizes* go out instead,
    because the sizes are what the reader is looking at.
    """
    if log is None:
        return WalResponse(
            enabled=False,
            path="",
            base_lsn=0,
            next_lsn=0,
            flushed_lsn=0,
            buffered_bytes=0,
            size_bytes=0,
            records=[],
            truncated_tail=False,
            total_records=0,
            stats=WalStatsModel(
                records_appended=0,
                records_coalesced=0,
                bytes_appended=0,
                flushes=0,
                syncs=0,
                mean_sync_ns=0.0,
                checkpoints=0,
                bytes_reclaimed=0,
            ),
        )

    records, truncated = log.read_all()
    stats = log.stats
    return WalResponse(
        enabled=True,
        # The filename, never the path. The workspace boundary is the reason
        # /health does the same with its own directory.
        path=log.path.name,
        base_lsn=log.base_lsn,
        next_lsn=log.next_lsn,
        flushed_lsn=log.flushed_lsn,
        buffered_bytes=log.buffered_bytes,
        size_bytes=size_bytes,
        records=[wal_record_to_api(record) for record in records[-limit:]],
        truncated_tail=truncated,
        total_records=len(records),
        stats=WalStatsModel(
            records_appended=stats.records_appended,
            records_coalesced=stats.records_coalesced,
            bytes_appended=stats.bytes_appended,
            flushes=stats.flushes,
            syncs=stats.syncs,
            mean_sync_ns=round(stats.mean_sync_ns, 1),
            checkpoints=stats.checkpoints,
            bytes_reclaimed=stats.bytes_reclaimed,
        ),
    )


def recovery_to_api(report: RecoveryReport) -> RecoveryReportModel:
    return RecoveryReportModel(
        ran=report.ran,
        records_scanned=report.records_scanned,
        truncated_tail=report.truncated_tail,
        winners=list(report.winners),
        losers=list(report.losers),
        pages_redone=report.pages_redone,
        pages_skipped=report.pages_skipped,
        pages_undone=report.pages_undone,
        highest_lsn=report.highest_lsn,
        duration_ns=report.duration_ns,
        phase_ns=dict(report.phase_ns),
        summary=report.summary(),
    )


def locks_to_api(table) -> LockTableResponse:
    """The lock table, with the graph as an adjacency list.

    ``readers_blocked`` is hard-coded to zero and shipped anyway. A field that
    is always zero looks like an oversight until you know why it must be: under
    MVCC a reader takes no lock, so there is nothing that *could* block one, and
    the API saying so is more useful than the API being silent.
    """
    return LockTableResponse(
        entries=[
            LockEntryModel(
                resource=entry.resource,
                holders={str(txn): mode.value for txn, mode in entry.holders.items()},
                waiters=[w.transaction_id for w in entry.waiters],
            )
            for entry in table.entries
        ],
        wait_for=[
            WaitForEdge(waiter=waiter, blockers=sorted(blockers))
            for waiter, blockers in sorted(table.wait_for.items())
        ],
        stats=LockStatsModel(
            granted=table.stats.granted,
            released=table.stats.released,
            waits=table.stats.waits,
            timeouts=table.stats.timeouts,
            deadlocks=table.stats.deadlocks,
        ),
        readers_blocked=0,
    )


def sessions_to_api(manager, locks) -> SessionListResponse:
    """Every session, whether or not it currently has a transaction.

    A session with nothing open still appears — a two-console view that made a
    console vanish between transactions would be unusable.
    """
    graph = locks.wait_for_graph()
    sessions = []
    for name in manager.sessions():
        transaction = manager.active_in(name)
        if transaction is None:
            sessions.append(SessionModel(session=name, transaction_id=None))
            continue
        snapshot = transaction.snapshot
        sessions.append(
            SessionModel(
                session=name,
                transaction_id=transaction.transaction_id,
                state=transaction.state.value,
                isolation_level=transaction.isolation.value,
                snapshot=snapshot.describe() if snapshot else None,
                snapshots_taken=transaction.snapshots_taken,
                statements=transaction.statements,
                rows_created=transaction.rows_created,
                rows_deleted=transaction.rows_deleted,
                locks_held=len(locks.held_by(transaction.transaction_id)),
                waiting_for=sorted(graph.get(transaction.transaction_id, ())),
            )
        )
    return SessionListResponse(
        sessions=sessions,
        frozen_xid=manager.frozen_xid,
        next_xid=manager.next_xid,
        oldest_snapshot_xmin=manager.oldest_snapshot_xmin(),
    )
