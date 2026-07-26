"""FastAPI dependencies and error translation."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request, status

from engine.errors import ChenDBError, SchemaError
from engine.server.config import ServerConfig
from engine.server.workspace import (
    DatabaseAlreadyExists,
    DatabaseNotFound,
    InvalidDatabaseId,
    ManagedDatabase,
    Workspace,
)

__all__ = [
    "ConfigDep",
    "DatabaseDep",
    "WorkspaceDep",
    "get_config",
    "get_database",
    "get_workspace",
    "http_status_for",
]

#: Engine and workspace errors mapped onto HTTP status codes. Anything not
#: listed becomes a 500, which is correct: an unmapped error is a bug.
_STATUS_MAP: dict[type[Exception], int] = {
    InvalidDatabaseId: status.HTTP_400_BAD_REQUEST,
    DatabaseNotFound: status.HTTP_404_NOT_FOUND,
    DatabaseAlreadyExists: status.HTTP_409_CONFLICT,
    SchemaError: status.HTTP_409_CONFLICT,
}

#: Literal rather than ``status.HTTP_422_*``: Starlette renamed that constant
#: and the old spelling now warns. The number is the stable part.
_UNPROCESSABLE = 422


def http_status_for(exc: Exception) -> int:
    for error_type, code in _STATUS_MAP.items():
        if isinstance(exc, error_type):
            return code
    if isinstance(exc, ChenDBError):
        return _UNPROCESSABLE
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def get_workspace(request: Request) -> Workspace:
    return request.app.state.workspace


def get_config(request: Request) -> ServerConfig:
    return request.app.state.config


WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]
ConfigDep = Annotated[ServerConfig, Depends(get_config)]


def get_database(
    workspace: WorkspaceDep,
    database_id: Annotated[
        str,
        Path(
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
            description="Workspace-relative database id",
        ),
    ],
) -> ManagedDatabase:
    """Resolve a path parameter to an open database, or raise 404.

    The pattern on the path parameter rejects traversal attempts before any
    filesystem call happens; :meth:`Workspace.path_for` checks containment
    again after resolution.
    """
    try:
        return workspace.get(database_id)
    except (DatabaseNotFound, InvalidDatabaseId) as exc:
        raise HTTPException(
            status_code=http_status_for(exc),
            detail={"error": type(exc).__name__, "message": str(exc)},
        ) from exc


DatabaseDep = Annotated[ManagedDatabase, Depends(get_database)]
