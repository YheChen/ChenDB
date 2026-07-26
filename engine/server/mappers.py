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
from collections.abc import Iterable, Sequence
from typing import Any

from engine.database import Database
from engine.diagnostics import SinkStats, TraceLevel, TraceRecord
from engine.serialization.record import FieldLayout, RecordLayout
from engine.serialization.schema import Column, Schema
from engine.server.schemas.database import (
    ColumnModel,
    DatabaseDetail,
    PagerStatsModel,
    RecordIdModel,
    RowModel,
    SchemaModel,
    TableResponse,
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
from engine.storage.constants import INVALID_PAGE_ID
from engine.storage.heap import RecordId
from engine.storage.inspect import HeaderField, PageDetail, PageSummary, SlotDetail
from engine.storage.pager import PagerStats

__all__ = [
    "column_to_api",
    "database_detail_to_api",
    "page_detail_to_api",
    "page_summary_to_api",
    "pager_stats_to_api",
    "record_id_to_api",
    "row_to_api",
    "schema_to_api",
    "table_to_api",
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
    row_count: int | None,
    heap_page_ids: Iterable[int],
    schema_page_ids: Iterable[int],
    trace_level: TraceLevel,
    file_size_bytes: int,
) -> DatabaseDetail:
    meta = db.pager.meta
    table = db.table
    return DatabaseDetail(
        database_id=db.database_id,
        page_size=db.page_size,
        page_count=db.page_count,
        file_size_bytes=file_size_bytes,
        format_version=meta.format_version,
        table_name=table.name if table else None,
        schema=schema_to_api(table.schema) if table else None,
        row_count=row_count,
        heap_page_ids=sorted(heap_page_ids),
        schema_page_ids=sorted(schema_page_ids),
        free_list_head=_optional_page_id(meta.free_list_head),
        stats=pager_stats_to_api(db.stats),
        trace_level=trace_level.name,
    )


def table_to_api(
    db: Database, *, row_count: int, heap_page_ids: Iterable[int]
) -> TableResponse:
    table = db.table
    assert table is not None  # callers check first and return 404 otherwise
    meta = db.pager.meta
    return TableResponse(
        name=table.name,
        schema=schema_to_api(table.schema),
        row_count=row_count,
        heap_page_ids=sorted(heap_page_ids),
        first_page_id=meta.heap_first_page,
        last_page_id=meta.heap_last_page,
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
