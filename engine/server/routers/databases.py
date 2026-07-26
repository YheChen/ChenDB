"""Database lifecycle, schema, and row endpoints.

Every handler here is a plain ``def``, not ``async def``.  Engine calls are
blocking file I/O; FastAPI runs synchronous handlers in a worker threadpool, so
one slow scan cannot stall the event loop and starve the WebSocket streams.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from engine.errors import ChenDBError
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.server import mappers
from engine.server.deps import DatabaseDep, WorkspaceDep, http_status_for
from engine.server.schemas.database import (
    CreateDatabaseRequest,
    CreateTableRequest,
    DatabaseDetail,
    DatabaseListResponse,
    DatabaseSummary,
    DeleteRecordResponse,
    InsertRecordsRequest,
    InsertRecordsResponse,
    RecordsResponse,
    TableResponse,
)
from engine.server.workspace import (
    DatabaseAlreadyExists,
    InvalidDatabaseId,
    ManagedDatabase,
    Workspace,
)
from engine.storage.heap import RecordId

router = APIRouter(prefix="/databases", tags=["databases"])

DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=http_status_for(exc),
        detail={"error": type(exc).__name__, "message": str(exc)},
    )


def _file_size(workspace: Workspace, database_id: str) -> int:
    path = workspace.path_for(database_id)
    return path.stat().st_size if path.exists() else 0


def _detail(managed: ManagedDatabase, workspace: Workspace) -> DatabaseDetail:
    """Build a database detail response from one consistent snapshot.

    The lock is held only while reading engine state into local variables; the
    Pydantic construction happens after it is released.
    """
    with managed.use() as db:
        row_count = db.count() if db.table else None
        heap_pages = db.heap_page_ids()
        schema_pages = db.schema_page_ids()
        detail = mappers.database_detail_to_api(
            db,
            row_count=row_count,
            heap_page_ids=heap_pages,
            schema_page_ids=schema_pages,
            trace_level=managed.tracer.level,
            file_size_bytes=_file_size(workspace, managed.database_id),
        )
    return detail


# -- lifecycle -------------------------------------------------------------


@router.get("", response_model=DatabaseListResponse, summary="List databases")
def list_databases(workspace: WorkspaceDep) -> DatabaseListResponse:
    return DatabaseListResponse(
        databases=[
            DatabaseSummary(
                database_id=entry.database_id,
                size_bytes=entry.size_bytes,
                modified_ns=entry.modified_ns,
                is_open=entry.is_open,
            )
            for entry in workspace.list_databases()
        ],
        # The directory name only — never an absolute path to the browser.
        workspace=workspace.root.name,
    )


@router.post(
    "",
    response_model=DatabaseDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a database",
)
def create_database(
    payload: CreateDatabaseRequest, workspace: WorkspaceDep
) -> DatabaseDetail:
    try:
        managed = workspace.create(payload.database_id, page_size=payload.page_size)
    except (DatabaseAlreadyExists, InvalidDatabaseId, ChenDBError, ValueError) as exc:
        raise _fail(exc) from exc
    return _detail(managed, workspace)


@router.get("/{database_id}", response_model=DatabaseDetail, summary="Database detail")
def get_database_detail(
    managed: DatabaseDep, workspace: WorkspaceDep
) -> DatabaseDetail:
    return _detail(managed, workspace)


@router.delete(
    "/{database_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a database file",
)
def delete_database(database_id: str, workspace: WorkspaceDep) -> None:
    try:
        workspace.delete(database_id)
    except ChenDBError as exc:
        raise _fail(exc) from exc


# -- schema ----------------------------------------------------------------


@router.post(
    "/{database_id}/table",
    response_model=TableResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Define the table (Milestone 1 allows one per database)",
)
def create_table(
    payload: CreateTableRequest, managed: DatabaseDep
) -> TableResponse:
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
            db.create_table(payload.name, schema)
            response = mappers.table_to_api(
                db, row_count=0, heap_page_ids=db.heap_page_ids()
            )
    except ChenDBError as exc:
        raise _fail(exc) from exc
    return response


@router.get(
    "/{database_id}/table", response_model=TableResponse, summary="Table and schema"
)
def get_table(managed: DatabaseDep) -> TableResponse:
    with managed.use() as db:
        if db.table is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "NoTable",
                    "message": "this database has no table yet; POST to /table first",
                },
            )
        response = mappers.table_to_api(
            db, row_count=db.count(), heap_page_ids=db.heap_page_ids()
        )
    return response


# -- rows ------------------------------------------------------------------


@router.post(
    "/{database_id}/records",
    response_model=InsertRecordsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Insert rows",
)
def insert_records(
    payload: InsertRecordsRequest, managed: DatabaseDep
) -> InsertRecordsResponse:
    try:
        with managed.use() as db:
            allocations_before = db.stats.allocations
            started = time.perf_counter_ns()
            record_ids = db.insert_many([list(row) for row in payload.rows])
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
    "/{database_id}/records", response_model=RecordsResponse, summary="Scan rows"
)
def list_records(
    managed: DatabaseDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_ROW_LIMIT)] = DEFAULT_ROW_LIMIT,
) -> RecordsResponse:
    """Return a window of the heap scan.

    There is no index and no ``LIMIT`` pushdown until later milestones, so
    ``offset`` is honoured by discarding rows the scan already produced. The
    response reports ``rows_scanned`` and ``pages_read`` precisely so that cost
    is visible rather than hidden.
    """
    with managed.use() as db:
        if db.table is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "NoTable",
                    "message": "this database has no table yet",
                },
            )
        columns = [mappers.column_to_api(column) for column in db.schema]
        reads_before = db.stats.page_reads
        started = time.perf_counter_ns()

        rows: list[Any] = []
        scanned = 0
        has_more = False
        for record_id, values in db.scan():
            scanned += 1
            if scanned <= offset:
                continue
            if len(rows) == limit:
                has_more = True
                break
            rows.append(mappers.row_to_api(record_id, values))

        duration = time.perf_counter_ns() - started
        pages_read = db.stats.page_reads - reads_before

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
    "/{database_id}/records/{page_id}/{slot_id}",
    response_model=DeleteRecordResponse,
    summary="Delete the row at a record id",
)
def delete_record(
    managed: DatabaseDep,
    page_id: Annotated[int, "heap page id"],
    slot_id: Annotated[int, "slot index within the page"],
) -> DeleteRecordResponse:
    record_id = RecordId(page_id, slot_id)
    try:
        with managed.use() as db:
            deleted = db.delete(record_id)
            db.sync()
    except ChenDBError as exc:
        raise _fail(exc) from exc
    return DeleteRecordResponse(
        deleted=deleted, record_id=mappers.record_id_to_api(record_id)
    )
