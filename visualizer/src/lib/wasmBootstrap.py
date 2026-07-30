"""The engine, started inside a browser tab.

This is the whole Python side of the WASM build. It runs once, under Pyodide,
and hands JavaScript two functions: one that answers a request, one that
subscribes to the event stream.

Nothing here is a reimplementation. The same routers, the same mappers, the
same Pydantic models and the same error envelope serve both builds — the ASGI
app is simply called in-process instead of over a socket. That is why
``api.ts`` can stay generated from one OpenAPI schema and why there is no
second endpoint layer to drift.

It is linted and formatted with the rest of the project. It lives beside the
transport that loads it rather than under ``engine/`` because the engine does
not know the web app exists, and this file is emphatically about the web app.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path
from typing import Any

import anyio.to_thread
import httpx
import starlette.concurrency

sys.path.insert(0, "/app")

import engine
from engine.errors import ChenDBError
from engine.server import app as app_module
from engine.server.config import ServerConfig
from engine.server.mappers import trace_record_to_api
from engine.storage.constants import FORMAT_VERSION

# --------------------------------------------------------------------------
# 1. Threads
# --------------------------------------------------------------------------


async def _inline(func, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Run what FastAPI wanted to put on a worker thread, right here.

    FastAPI offloads a **synchronous** route handler so a slow one cannot block
    the event loop, and every ChenDB router is ``def`` rather than ``async
    def``. WASM has no threads, so without this the very first request dies
    with ``RuntimeError: can't start new thread``.

    Safe *here*, and only here: one tab, one caller, nothing to be concurrent
    with. The tempting alternative — making the twenty-five routers ``async
    def`` — would push this constraint onto the real server, where it would
    serialise every database behind every other one. The browser's problem
    should stay the browser's.
    """
    return func(*args)


anyio.to_thread.run_sync = _inline
starlette.concurrency.run_in_threadpool = lambda func, *a, **k: _inline(
    functools.partial(func, **k) if k else func, *a
)

# --------------------------------------------------------------------------
# 2. The app
# --------------------------------------------------------------------------

WORKSPACE = Path("/workspace")

#: Step mode blocks a worker thread at each checkpoint and resumes it from
#: another request. There is nowhere to block, so it is advertised as absent
#: rather than left to fail when someone presses the button — the same rule
#: every unbuilt feature has followed since Milestone 1.
app_module.FEATURES["execution_stepping"] = False

#: The crash button really does work: abandoning the pager loses the buffer
#: pool, and reopening replays the log out of the in-memory filesystem. What it
#: cannot demonstrate is a power cut, because ``fsync`` here has nothing to
#: sync to. The UI says so rather than letting the demo overstate itself.
app_module.FEATURES["durable_fsync"] = False


def _create_app() -> Any:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return app_module.create_app(ServerConfig(workspace=WORKSPACE))


_app = _create_app()
_lifespan: Any = None


#: What the persisted workspace was written by. A database is a *binary* file
#: with a version in its meta page, so a format bump makes yesterday's stored
#: bytes unopenable — and an unopenable database in IndexedDB is a demo that is
#: permanently broken for that visitor, with no way for them to know why.
#: JavaScript reads this at boot and clears the store when it does not match.
STAMP = Path("/workspace/.chendb-format")


def format_version() -> str:
    """``engine-format`` marker for the persisted workspace.

    The *format* version, not the engine version: 1.5.0 to 1.6.0 need not
    change a single byte on disk, and clearing a visitor's databases because
    the UI changed would be gratuitous.
    """
    return f"{FORMAT_VERSION}"


def stored_version() -> str:
    """What is already in the store, or ``""`` if nothing is."""
    try:
        return STAMP.read_text().strip()
    except OSError:
        return ""


def stamp() -> None:
    STAMP.write_text(format_version())


async def start() -> str:
    """Enter the app's lifespan. Returns a one-line banner for the console."""
    global _lifespan
    _lifespan = _app.router.lifespan_context(_app)
    await _lifespan.__aenter__()
    stamp()
    return f"ChenDB {engine.__version__} (milestone {engine.MILESTONE}) in WebAssembly"


def close() -> None:
    """Flush every open handle so the bytes on the filesystem are current.

    Persisting means copying the *filesystem* into IndexedDB, and a page still
    sitting in the buffer pool is not on the filesystem yet. Without this, the
    thing that gets stored is whatever last happened to be written through —
    which is exactly the state recovery exists to repair, and not what a visitor
    who typed a statement and closed the tab is entitled to.
    """
    _app.state.workspace.close_all()


async def handle(method: str, path: str, body: str | None) -> str:
    """One request. Returns ``{"status": int, "body": <parsed json or null>}``.

    JSON in and JSON out because the boundary to JavaScript is a string either
    way, and going through the real ASGI stack means Pydantic validation and
    the error envelope behave exactly as they do over HTTP — including the
    422s, which a hand-rolled dispatcher would have quietly skipped.

    ``isinstance(body, str)`` rather than ``body is not None``: JavaScript's
    ``null`` arrives here as a ``JsNull`` object, which is emphatically not
    ``None`` and fails the ``is not None`` test while having no ``.encode``.
    Asking what the value *is* rather than what it is not works whichever way
    the caller spells "no body".
    """
    content = body.encode() if isinstance(body, str) else None
    transport = httpx.ASGITransport(app=_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://wasm") as client:
        response = await client.request(
            method,
            path,
            content=content,
            headers={"content-type": "application/json"} if content else None,
        )
    text = response.text
    return json.dumps(
        {"status": response.status_code, "body": json.loads(text) if text else None}
    )


# --------------------------------------------------------------------------
# 3. Events, without a socket
# --------------------------------------------------------------------------


class _CallbackSink:
    """A diagnostic sink that hands each record straight to JavaScript.

    The HTTP build reaches the same records through a WebSocket, a bounded
    queue and a batching writer, all of which exist to get bytes across a
    network without unbounded buffering. None of that applies to a function
    call, so this is the whole of the event stream here — and it is why the
    reconnection logic moved into the HTTP transport where it belongs.
    """

    __slots__ = ("_emit",)

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    def record(self, item: Any) -> None:
        self._emit(trace_record_to_api(item).model_dump_json())


_sinks: dict[str, _CallbackSink] = {}


def _managed(database_id: str) -> Any:
    """The open handle, or ``None`` if there is no such database.

    ``Workspace.get`` raises for an unknown id, which is right for a request —
    the client gets a 404 — and wrong here: subscribing to a database the user
    has not created yet is an ordinary thing for the UI to try while it is
    still deciding which one to show.
    """
    try:
        return _app.state.workspace.get(database_id)
    except ChenDBError:
        return None


def subscribe(database_id: str, emit: Any) -> None:
    """Start delivering ``database_id``'s events to ``emit``."""
    unsubscribe(database_id)
    managed = _managed(database_id)
    if managed is None:
        return
    sink = _CallbackSink(emit)
    _sinks[database_id] = sink
    managed.subscribe(sink)


def unsubscribe(database_id: str) -> None:
    sink = _sinks.pop(database_id, None)
    if sink is None:
        return
    managed = _managed(database_id)
    if managed is not None:
        managed.unsubscribe(sink)
