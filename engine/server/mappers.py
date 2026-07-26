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

from engine.catalog.catalog import CatalogStats, TableInfo
from engine.database import Database
from engine.diagnostics import SinkStats, TraceLevel, TraceRecord
from engine.errors import SqlError
from engine.executor.binder import ResultColumn
from engine.executor.engine import QueryResult
from engine.executor.operators import Operator
from engine.parser.analyze import ParseOutcome
from engine.parser.ast import Node, walk
from engine.parser.tokens import Token
from engine.serialization.record import FieldLayout, RecordLayout
from engine.serialization.schema import Column, Schema
from engine.server.executions import Execution
from engine.server.schemas.catalog import (
    CatalogStatsModel,
    TableDetail,
    TableStorageModel,
    TableSummary,
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
    PlanModel,
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
from engine.storage.constants import INVALID_PAGE_ID
from engine.storage.heap import RecordId
from engine.storage.inspect import HeaderField, PageDetail, PageSummary, SlotDetail
from engine.storage.pager import PagerStats

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
    return [
        bool(bitmap[index // 8] >> (index % 8) & 1) for index in range(column_count)
    ]


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
        tokens=[
            token_to_api(index, token) for index, token in enumerate(outcome.tokens)
        ],
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


def _operator_nodes(root: Operator) -> list[OperatorNodeModel]:
    """Flatten an operator tree, parents before children."""
    nodes: list[OperatorNodeModel] = []
    stack = [root]
    while stack:
        operator = stack.pop()
        nodes.append(
            OperatorNodeModel(
                operator_id=operator.operator_id,
                operator_type=operator.operator_type,
                detail=operator.detail,
                children=[child.operator_id for child in operator.children],
                output_columns=[
                    result_column_to_api(column) for column in operator.output_columns
                ],
                next_calls=operator.stats.next_calls,
                input_rows=operator.stats.input_rows,
                output_rows=operator.stats.output_rows,
                rows_rejected=getattr(operator, "rows_rejected", 0),
                duration_ns=operator.stats.duration_ns,
            )
        )
        stack.extend(reversed(operator.children))
    return nodes


def plan_to_api(root: Operator) -> PlanModel:
    return PlanModel(nodes=_operator_nodes(root), root_id=root.operator_id)


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
        plan=plan_to_api(result.plan) if result.plan is not None else None,
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
        plan=plan_to_api(plan) if plan is not None else None,
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
    )
