"""FastAPI application factory.

This module is the *only* place where the engine meets the web.  Everything
below ``engine/`` that is not under ``engine/server/`` is pure standard-library
Python and has no idea this file exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine import __version__ as engine_version
from engine.errors import ChenDBError
from engine.server.config import ServerConfig, load_config
from engine.server.deps import http_status_for
from engine.server.executions import ExecutionStore
from engine.server.routers import databases, events, pages, query, sql
from engine.server.schemas.common import ApiError, HealthResponse
from engine.server.workspace import Workspace

__all__ = ["API_PREFIX", "API_VERSION", "MILESTONE", "create_app"]

API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"

#: Highest completed milestone. The frontend reads this from /health and hides
#: panels for anything not built yet, rather than showing dead controls.
MILESTONE = 3

#: Advertised capabilities. Each flips to true in the milestone that ships it.
FEATURES: dict[str, bool] = {
    "storage": True,
    "page_inspector": True,
    "diagnostics": True,
    "event_stream": True,
    "sql": True,           # Milestone 2 — parsing only, no execution
    "execution": True,     # Milestone 3 — volcano operators + step mode
    "catalog": False,      # Milestone 4
    "indexes": False,      # Milestone 5
    "planner": False,      # Milestone 6
    "buffer_pool": False,  # Milestone 7
    "transactions": False, # Milestone 8
    "wal": False,          # Milestone 9
    "mvcc": False,         # Milestone 10
}

_DESCRIPTION = """
HTTP and WebSocket access to a running ChenDB engine, for the Visual Database
Explorer.

Everything served here is real engine state read from a real database file.
Endpoints for features that do not exist yet are absent rather than stubbed:
check `features` on `/health` to see what this build supports.
""".strip()


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Build the API. Tests call this directly with a temporary workspace."""
    resolved = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = resolved
        app.state.workspace = Workspace(resolved)
        app.state.executions = ExecutionStore(resolved)
        try:
            yield
        finally:
            # Stepped executions hold engine threads and database locks. Cancel
            # them before closing databases, or close_all() would block on a
            # lock a paused query still owns.
            app.state.executions.shutdown()
            # Closing each handle fsyncs it. Without this, a server stopped
            # with Ctrl-C could leave recent writes only in the OS page cache.
            app.state.workspace.close_all()

    app = FastAPI(
        title="ChenDB Engine API",
        description=_DESCRIPTION,
        version=engine_version,
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(ChenDBError)
    async def handle_engine_error(request: Request, exc: ChenDBError) -> JSONResponse:
        """Turn any engine error into the uniform error envelope.

        A leaked traceback would be both a poor client experience and an
        information disclosure, since engine messages can contain paths.
        """
        return JSONResponse(
            status_code=http_status_for(exc),
            content=ApiError(
                error=type(exc).__name__, message=str(exc)
            ).model_dump(),
        )

    @app.get(
        f"{API_PREFIX}/health",
        response_model=HealthResponse,
        tags=["meta"],
        summary="Engine version, milestone and feature flags",
    )
    def health(request: Request) -> HealthResponse:
        workspace: Workspace = request.app.state.workspace
        return HealthResponse(
            engine_version=engine_version,
            api_version=API_VERSION,
            milestone=MILESTONE,
            workspace=workspace.root.name,
            open_databases=workspace.open_count,
            features=FEATURES,
        )

    app.include_router(databases.router, prefix=API_PREFIX)
    app.include_router(pages.router, prefix=API_PREFIX)
    app.include_router(events.router, prefix=API_PREFIX)
    app.include_router(sql.router, prefix=API_PREFIX)
    app.include_router(query.router, prefix=API_PREFIX)
    app.include_router(query.executions_router, prefix=API_PREFIX)
    return app


#: Module-level app for ``uvicorn engine.server.app:app``.
app = create_app()
