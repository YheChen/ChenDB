"""Catalog API models.

The schema browser needs three things the storage view cannot give it: what
tables exist, what shape each one is, and how much space each is actually using.
"""

from __future__ import annotations

from pydantic import Field

from engine.server.schemas.common import ApiModel, RequestModel
from engine.server.schemas.database import ColumnModel, ColumnSpec, SchemaModel

__all__ = [
    "CatalogResponse",
    "CatalogStatsModel",
    "CreateTableRequest",
    "TableDetail",
    "TableStorageModel",
    "TableSummary",
]


class TableStorageModel(ApiModel):
    """What a table costs on disk. Computed, not cached."""

    first_page: int
    last_page: int
    page_ids: list[int]
    page_count: int
    row_count: int = Field(
        description="Rows a reader can see. O(pages) to compute; no cached count."
    )
    version_count: int = Field(
        description=(
            "Row versions physically present, including ones only an older "
            "snapshot could still want. The gap from row_count is what a vacuum "
            "would reclaim."
        )
    )
    bytes_allocated: int = Field(description="page_count * page_size")
    free_space: int = Field(description="Contiguous free bytes across the table's pages")
    reclaimable_space: int = Field(
        description="Bytes held by tombstoned rows, recoverable by compaction"
    )


class TableSummary(ApiModel):
    table_id: int
    name: str
    column_count: int
    row_count: int = Field(description="Rows a reader can see, not versions on disk")
    page_count: int
    is_system: bool = Field(
        description="True for chendb_* tables, which belong to the engine"
    )


class TableDetail(ApiModel):
    table_id: int
    name: str
    is_system: bool
    schema_: SchemaModel = Field(alias="schema")
    columns: list[ColumnModel]
    storage: TableStorageModel


class CatalogStatsModel(ApiModel):
    """Catalog cache effectiveness. A miss costs two full catalog scans."""

    lookups: int
    cache_hits: int
    hit_rate: float
    scans: int
    tables_created: int
    indexes_created: int


class CatalogResponse(ApiModel):
    tables: list[TableSummary]
    system_tables: list[TableSummary]
    next_object_id: int = Field(
        description="Id the next created table or index will receive; one "
        "sequence serves both, as PostgreSQL's OID counter does"
    )
    stats: CatalogStatsModel


class CreateTableRequest(RequestModel):
    """Programmatic table creation.

    ``CREATE TABLE`` through ``POST /query`` is the primary path from Milestone 3
    onward; this stays for clients that would rather not build SQL strings.
    """

    name: str = Field(min_length=1, max_length=64)
    columns: list[ColumnSpec] = Field(min_length=1)
