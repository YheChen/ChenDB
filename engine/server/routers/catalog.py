"""Catalog endpoints: what tables exist, and what each one costs.

Replaces Milestone 1's singular ``/table``.  A database holds many tables now, so
the resource is a collection:

    GET    /databases/{db}/catalog              tables + cache statistics
    GET    /databases/{db}/tables               summaries
    POST   /databases/{db}/tables               create one
    GET    /databases/{db}/tables/{table}       schema + storage detail
    GET    /databases/{db}/tables/{table}/records
    POST   /databases/{db}/tables/{table}/records
    DELETE /databases/{db}/tables/{table}/records/{page}/{slot}

The storage figures are computed on request by walking the table's page chain.
That is O(pages) reads, which is fine for a browser refresh and honest about the
fact that nothing is cached — PostgreSQL's ``reltuples`` is an *estimate*
maintained by ``ANALYZE`` precisely because keeping it exact would cost a write
on every insert.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from engine.catalog.catalog import TableInfo
from engine.errors import ChenDBError
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.server import mappers
from engine.server.deps import DatabaseDep, http_status_for
from engine.server.schemas.catalog import (
    CatalogResponse,
    CreateTableRequest,
    TableDetail,
    TableStorageModel,
    TableSummary,
)
from engine.server.schemas.database import (
    DeleteRecordResponse,
    InsertRecordsRequest,
    InsertRecordsResponse,
    RecordsResponse,
)
from engine.server.workspace import ManagedDatabase
from engine.storage.heap import RecordId

router = APIRouter(prefix="/databases/{database_id}", tags=["catalog"])

DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=http_status_for(exc),
        detail={"error": type(exc).__name__, "message": str(exc)},
    )


def _storage_of(managed: ManagedDatabase, db: Any, info: TableInfo) -> TableStorageModel:
    """Walk a table's page chain and total up what it is using.

    Caller must hold the engine lock; this reads pages.
    """
    heap = db.heap_for(info.name)
    page_ids = list(heap.page_ids())
    free_space = 0
    reclaimable = 0
    for page_id in page_ids:
        page = db.read_page(page_id)
        free_space += page.free_space
        reclaimable += page.reclaimable_space
    return mappers.table_storage_to_api(
        info,
        page_ids=page_ids,
        row_count=heap.count(),
        page_size=db.page_size,
        free_space=free_space,
        reclaimable_space=reclaimable,
    )


def _summary_of(db: Any, info: TableInfo) -> TableSummary:
    heap = db.heap_for(info.name)
    return mappers.table_summary_to_api(
        info, row_count=heap.count(), page_count=heap.page_count()
    )


# -- catalog ---------------------------------------------------------------


@router.get(
    "/catalog",
    response_model=CatalogResponse,
    summary="Every table, plus catalog cache statistics",
)
def get_catalog(managed: DatabaseDep) -> CatalogResponse:
    with managed.use() as db:
        user = [_summary_of(db, info) for info in db.tables()]
        system = [
            _summary_of(db, info)
            for info in db.tables(include_system=True)
            if info.is_system
        ]
        next_object_id = db.pager.meta.next_object_id
        stats = mappers.catalog_stats_to_api(db.catalog.stats)

    return CatalogResponse(
        tables=user,
        system_tables=system,
        next_object_id=next_object_id,
        stats=stats,
    )


# -- tables ----------------------------------------------------------------


@router.get("/tables", response_model=list[TableSummary], summary="List tables")
def list_tables(
    managed: DatabaseDep,
    include_system: Annotated[bool, Query()] = False,
) -> list[TableSummary]:
    with managed.use() as db:
        return [_summary_of(db, info) for info in db.tables(include_system=include_system)]


@router.post(
    "/tables",
    response_model=TableDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a table",
)
def create_table(payload: CreateTableRequest, managed: DatabaseDep) -> TableDetail:
    try:
        schema = Schema(
            tuple(
                Column(
                    name=spec.name,
                    data_type=DataType.from_sql_name(spec.type),
                    nullable=spec.nullable and not spec.primary_key,
                    primary_key=spec.primary_key,
                )
                for spec in payload.columns
            )
        )
    except (ChenDBError, ValueError) as exc:
        raise _fail(exc) from exc

    try:
        with managed.use() as db:
            info = db.create_table(payload.name, schema)
            detail = mappers.table_detail_to_api(info, _storage_of(managed, db, info))
    except ChenDBError as exc:
        raise _fail(exc) from exc
    return detail


@router.get(
    "/tables/{table}",
    response_model=TableDetail,
    summary="A table's schema and storage statistics",
)
def get_table(managed: DatabaseDep, table: str) -> TableDetail:
    try:
        with managed.use() as db:
            info = db.require_table(table)
            detail = mappers.table_detail_to_api(info, _storage_of(managed, db, info))
    except ChenDBError as exc:
        raise _fail(exc) from exc
    return detail


# -- rows ------------------------------------------------------------------


@router.post(
    "/tables/{table}/records",
    response_model=InsertRecordsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Insert rows into a table",
)
def insert_records(
    payload: InsertRecordsRequest, managed: DatabaseDep, table: str
) -> InsertRecordsResponse:
    import time

    try:
        with managed.use() as db:
            allocations_before = db.stats.allocations
            started = time.perf_counter_ns()
            record_ids = db.insert_many(table, [list(row) for row in payload.rows])
            duration = time.perf_counter_ns() - started
            pages_allocated = db.stats.allocations - allocations_before
            db.sync()
    except ChenDBError as exc:
        raise _fail(exc) from exc

    return InsertRecordsResponse(
        inserted=len(record_ids),
        record_ids=[mappers.record_id_to_api(rid) for rid in record_ids],
        pages_allocated=pages_allocated,
        duration_ns=duration,
    )


@router.get(
    "/tables/{table}/records",
    response_model=RecordsResponse,
    summary="Scan a table's rows",
)
def list_records(
    managed: DatabaseDep,
    table: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_ROW_LIMIT)] = DEFAULT_ROW_LIMIT,
) -> RecordsResponse:
    """Return a window of a heap scan.

    There is still no index and no ``LIMIT`` pushdown, so ``offset`` is honoured
    by discarding rows the scan already produced. ``rows_scanned`` and
    ``pages_read`` report that cost rather than hiding it.
    """
    import time

    try:
        with managed.use() as db:
            info = db.require_table(table)
            columns = [mappers.column_to_api(column) for column in info.schema]
            reads_before = db.stats.page_reads
            started = time.perf_counter_ns()

            rows: list[Any] = []
            scanned = 0
            has_more = False
            for record_id, values in db.scan(info.name):
                scanned += 1
                if scanned <= offset:
                    continue
                if len(rows) == limit:
                    has_more = True
                    break
                rows.append(mappers.row_to_api(record_id, values))

            duration = time.perf_counter_ns() - started
            pages_read = db.stats.page_reads - reads_before
    except ChenDBError as exc:
        raise _fail(exc) from exc

    return RecordsResponse(
        columns=columns,
        rows=rows,
        offset=offset,
        limit=limit,
        returned=len(rows),
        has_more=has_more,
        rows_scanned=scanned,
        pages_read=pages_read,
        duration_ns=duration,
    )


@router.delete(
    "/tables/{table}/records/{page_id}/{slot_id}",
    response_model=DeleteRecordResponse,
    summary="Delete the row at a record id",
)
def delete_record(
    managed: DatabaseDep, table: str, page_id: int, slot_id: int
) -> DeleteRecordResponse:
    record_id = RecordId(page_id, slot_id)
    try:
        with managed.use() as db:
            deleted = db.delete(table, record_id)
            db.sync()
    except ChenDBError as exc:
        raise _fail(exc) from exc
    return DeleteRecordResponse(
        deleted=deleted, record_id=mappers.record_id_to_api(record_id)
    )
