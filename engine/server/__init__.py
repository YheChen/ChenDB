"""HTTP and WebSocket access to the engine, for the Visual Database Explorer.

This is the ONLY package under ``engine/`` allowed to import FastAPI, Pydantic
or uvicorn. A rule enforced by ``tests/unit/test_architecture_boundaries.py``.
The dependency points one way: the server imports the engine, never the
reverse, so ``import engine`` works in an environment with nothing installed.

    python -m engine.server            # start on 127.0.0.1:8000
    python -m engine.server --reload   # development mode

Layout::

    config.py     ServerConfig, read from CHENDB_* environment variables
    workspace.py  database lifecycle, path containment, the engine lock
    deps.py       FastAPI dependencies and error -> HTTP status mapping
    schemas/      Pydantic wire models
    mappers.py    engine dataclass -> wire model. The boundary.
    routers/      endpoint definitions
    app.py        the application factory
"""

from engine.server.app import API_PREFIX, API_VERSION, MILESTONE, create_app
from engine.server.config import ServerConfig, load_config

__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "MILESTONE",
    "ServerConfig",
    "create_app",
    "load_config",
]
