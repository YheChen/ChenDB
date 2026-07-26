"""Pydantic models for the HTTP and WebSocket API.

These are the *wire* types.  They exist only in ``engine/server`` and are built
from engine dataclasses by :mod:`engine.server.mappers`.  Keeping them separate
means the on-disk format and the on-the-wire format can evolve independently —
adding a field to a page header does not silently change the API, and renaming
an API field does not touch storage code.

Every model sets ``extra="forbid"`` on request bodies, so a typo in a client
payload is a 422 rather than a silently ignored field.
"""

from engine.server.schemas.catalog import (
    CatalogResponse,
    CatalogStatsModel,
    CreateTableRequest,
    TableDetail,
    TableStorageModel,
    TableSummary,
)
from engine.server.schemas.common import (
    ApiError,
    HealthResponse,
    PageInfo,
    PaginationMeta,
)
from engine.server.schemas.database import (
    ColumnModel,
    CreateDatabaseRequest,
    DatabaseDetail,
    DatabaseListResponse,
    DatabaseSummary,
    DeleteRecordResponse,
    InsertRecordsRequest,
    InsertRecordsResponse,
    PagerStatsModel,
    RecordIdModel,
    RecordsResponse,
    RowModel,
    SchemaModel,
)
from engine.server.schemas.events import (
    EventsResponse,
    SetTraceLevelRequest,
    TraceLevelResponse,
    TraceRecordModel,
    TraceStatsModel,
    WsClientMessage,
    WsDroppedMessage,
    WsEventsMessage,
    WsHelloMessage,
)
from engine.server.schemas.pages import (
    FieldLayoutModel,
    HeaderFieldModel,
    PageDetailModel,
    PageListResponse,
    PageSummaryModel,
    RecordLayoutModel,
    SlotDetailModel,
)

__all__ = [
    "ApiError",
    "CatalogResponse",
    "CatalogStatsModel",
    "ColumnModel",
    "CreateDatabaseRequest",
    "CreateTableRequest",
    "DatabaseDetail",
    "DatabaseListResponse",
    "DatabaseSummary",
    "DeleteRecordResponse",
    "EventsResponse",
    "FieldLayoutModel",
    "HeaderFieldModel",
    "HealthResponse",
    "InsertRecordsRequest",
    "InsertRecordsResponse",
    "PageDetailModel",
    "PageInfo",
    "PageListResponse",
    "PageSummaryModel",
    "PagerStatsModel",
    "PaginationMeta",
    "RecordIdModel",
    "RecordLayoutModel",
    "RecordsResponse",
    "RowModel",
    "SchemaModel",
    "SetTraceLevelRequest",
    "SlotDetailModel",
    "TableDetail",
    "TableStorageModel",
    "TableSummary",
    "TraceLevelResponse",
    "TraceRecordModel",
    "TraceStatsModel",
    "WsClientMessage",
    "WsDroppedMessage",
    "WsEventsMessage",
    "WsHelloMessage",
]
