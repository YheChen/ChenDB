"""Server configuration.

Read from environment variables prefixed ``CHENDB_``.  Deliberately a plain
frozen dataclass rather than Pydantic settings: configuration is loaded once at
startup and the validation it needs is a handful of range checks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from engine.diagnostics.levels import TraceLevel

__all__ = ["ServerConfig", "load_config"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_WORKSPACE = Path("workspace")

#: Vite's dev server. Only these origins may call the API from a browser.
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Everything the server needs to know at startup."""

    workspace: Path = DEFAULT_WORKSPACE
    """Directory holding database files. The only path the API can reach."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    default_trace_level: TraceLevel = TraceLevel.STORAGE
    """Databases open at this level so the visualizer has events immediately."""

    trace_capacity: int = 20_000
    """Events retained per database. Bounded: a VERBOSE scan of a big table
    can emit millions, and an unbounded buffer is a memory leak."""

    max_open_databases: int = 16
    """Open handles held at once. Each pins a file descriptor and a ring buffer."""

    websocket_queue_size: int = 512
    """Per-connection backlog. A client slower than the engine has its oldest
    queued events dropped, and is told how many."""

    websocket_batch_size: int = 64
    """Events coalesced into one frame, to avoid one message per page read."""

    max_page_bytes_returned: int = 65_536
    """Guard against a pathological page size flooding a JSON response."""

    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS

    def __post_init__(self) -> None:
        if self.trace_capacity < 1:
            raise ValueError("CHENDB_TRACE_CAPACITY must be at least 1")
        if self.websocket_queue_size < 1:
            raise ValueError("CHENDB_WS_QUEUE_SIZE must be at least 1")
        if self.max_open_databases < 1:
            raise ValueError("CHENDB_MAX_OPEN_DATABASES must be at least 1")

    @property
    def workspace_path(self) -> Path:
        return self.workspace.expanduser().resolve()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None else int(raw)


def load_config() -> ServerConfig:
    """Build a config from the environment, falling back to the defaults."""
    origins = os.environ.get("CHENDB_CORS_ORIGINS")
    level_name = os.environ.get("CHENDB_TRACE_LEVEL", "STORAGE").upper()
    return ServerConfig(
        workspace=Path(os.environ.get("CHENDB_WORKSPACE", str(DEFAULT_WORKSPACE))),
        host=os.environ.get("CHENDB_HOST", DEFAULT_HOST),
        port=_env_int("CHENDB_PORT", DEFAULT_PORT),
        default_trace_level=TraceLevel[level_name],
        trace_capacity=_env_int("CHENDB_TRACE_CAPACITY", 20_000),
        max_open_databases=_env_int("CHENDB_MAX_OPEN_DATABASES", 16),
        websocket_queue_size=_env_int("CHENDB_WS_QUEUE_SIZE", 512),
        cors_origins=(
            tuple(origin.strip() for origin in origins.split(",") if origin.strip())
            if origins
            else DEFAULT_CORS_ORIGINS
        ),
    )
