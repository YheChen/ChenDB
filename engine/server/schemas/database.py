"""Database, schema and record API models."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from engine.server.schemas.common import ApiModel, RequestModel
from engine.storage.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MIN_PAGE_SIZE

__all__ = [
    "ColumnModel",
    "CreateDatabaseRequest",
    "CreateTableRequest",
    "DatabaseDetail",
    "DatabaseListResponse",
    "DatabaseSummary",
    "DeleteRecordResponse",
    "InsertRecordsRequest",
    "InsertRecordsResponse",
    "PagerStatsModel",
    "RecordIdModel",
    "RecordsResponse",
    "RowModel",
    "SchemaModel",
]

#: Mirrors engine.serialization.types.DataType. A Literal rather than an import
#: of the IntEnum so the generated OpenAPI schema — and therefore the generated
#: TypeScript — is a readable string union.
SqlTypeName = Literal["INTEGER", "FLOAT", "BOOLEAN", "TEXT"]

DatabaseId = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description="Workspace-relative identifier. Never a filesystem path.",
        examples=["demo"],
    ),
]


class ColumnModel(ApiModel):
    name: str
    type: SqlTypeName
    nullable: bool
    primary_key: bool
    fixed_size: int | None = Field(
        description="Encoded width in bytes, or null for variable-width types"
    )


class SchemaModel(ApiModel):
    columns: list[ColumnModel]
    null_bitmap_size: int = Field(description="Bytes of null bitmap per record")
    fixed_row_size: int | None = Field(
        description="Constant encoded row size, or null if any column varies"
    )


class CreateDatabaseRequest(RequestModel):
    database_id: DatabaseId
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=MIN_PAGE_SIZE,
        le=MAX_PAGE_SIZE,
        description=(
            "Bytes per page; must be a power of two. Small values are useful "
            "for demos because they force page chaining after a few rows."
        ),
    )


class ColumnSpec(RequestModel):
    name: str = Field(min_length=1, max_length=64)
    type: SqlTypeName
    nullable: bool = True
    primary_key: bool = False


class CreateTableRequest(RequestModel):
    """Milestone 1 has no SQL parser, so tables are defined structurally.

    Milestone 2 adds ``POST /query`` with ``CREATE TABLE``; this endpoint stays
    as the programmatic path.
    """

    name: str = Field(min_length=1, max_length=64)
    columns: list[ColumnSpec] = Field(min_length=1)


class PagerStatsModel(ApiModel):
    """Cumulative I/O counters since the database handle was opened."""

    page_reads: int
    page_writes: int
    allocations: int
    recycled_allocations: int
    frees: int
    syncs: int
    bytes_read: int
    bytes_written: int
    read_time_ns: int
    write_time_ns: int


class DatabaseSummary(ApiModel):
    database_id: str
    size_bytes: int
    modified_ns: int
    is_open: bool


class DatabaseListResponse(ApiModel):
    databases: list[DatabaseSummary]
    workspace: str


class DatabaseDetail(ApiModel):
    database_id: str
    page_size: int
    page_count: int
    file_size_bytes: int
    format_version: int
    table_names: list[str] = Field(
        description="User tables. System tables are listed by /catalog."
    )
    table_count: int
    free_list_head: int | None
    stats: PagerStatsModel
    trace_level: str


class RecordIdModel(ApiModel):
    """PostgreSQL calls this a ctid: the physical address of a row."""

    page_id: int
    slot_id: int


class RowModel(ApiModel):
    record_id: RecordIdModel
    values: list[Any] = Field(
        description="Positional values matching the table's column order; null is NULL"
    )


class InsertRecordsRequest(RequestModel):
    rows: list[list[Any]] = Field(
        min_length=1,
        max_length=10_000,
        description="Positional values per row, in column order",
    )


class InsertRecordsResponse(ApiModel):
    inserted: int
    record_ids: list[RecordIdModel]
    pages_allocated: int
    duration_ns: int


class RecordsResponse(ApiModel):
    columns: list[ColumnModel]
    rows: list[RowModel]
    offset: int
    limit: int
    returned: int
    has_more: bool
    rows_scanned: int = Field(
        description="Rows the heap scan touched, including those skipped by offset"
    )
    pages_read: int = Field(description="Page reads this request caused")
    duration_ns: int


class DeleteRecordResponse(ApiModel):
    deleted: bool
    record_id: RecordIdModel
