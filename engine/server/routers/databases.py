"""Database lifecycle, schema, and row endpoints.

Every handler here is a plain ``def``, not ``async def``.  Engine calls are
blocking file I/O; FastAPI runs synchronous handlers in a worker threadpool, so
one slow scan cannot stall the event loop and starve the WebSocket streams.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from engine.errors import ChenDBError
from engine.server import mappers
from engine.server.deps import DatabaseDep, WorkspaceDep, http_status_for
from engine.server.schemas.database import (
    CreateDatabaseRequest,
    DatabaseDetail,
    DatabaseListResponse,
    DatabaseSummary,
)
from engine.server.workspace import (
    DatabaseAlreadyExists,
    InvalidDatabaseId,
    ManagedDatabase,
    Workspace,
)

router = APIRouter(prefix="/databases", tags=["databases"])


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
        detail = mappers.database_detail_to_api(
            db,
            tables=db.tables(),
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
        # The directory name only: never an absolute path to the browser.
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
def get_database_detail(managed: DatabaseDep, workspace: WorkspaceDep) -> DatabaseDetail:
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
