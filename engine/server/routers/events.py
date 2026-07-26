"""Diagnostic-event endpoints: history over HTTP, live stream over WebSocket.

Backpressure
------------
The engine emits events from whichever thread is doing the work.  A WebSocket
client consumes them on the event loop.  If the client is slower than the
engine — always true during a large scan at ``VERBOSE`` — something has to give.

The policy is **drop oldest, and say so**.  Each connection owns a bounded
queue; when it is full the oldest queued event is discarded and a counter
increments, which is reported to the client in a ``dropped`` frame.  The
alternative, blocking the producer, would mean a browser tab that stopped
reading could stall a query inside the storage engine. A diagnostics channel
must never be able to do that.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from engine.diagnostics import CallbackSink, TraceLevel, TraceRecord
from engine.diagnostics.levels import EventCategory
from engine.server import mappers
from engine.server.deps import ConfigDep, DatabaseDep, get_workspace
from engine.server.schemas.common import PageInfo
from engine.server.schemas.events import (
    EventsResponse,
    SetTraceLevelRequest,
    TraceLevelResponse,
    WsClientMessage,
    WsDroppedMessage,
    WsErrorMessage,
    WsEventsMessage,
    WsHelloMessage,
)
from engine.server.workspace import (
    DatabaseNotFound,
    InvalidDatabaseId,
    ManagedDatabase,
)

router = APIRouter(prefix="/databases/{database_id}", tags=["diagnostics"])

DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 2000

#: How long the writer waits for more events before flushing a partial batch.
_BATCH_LINGER_SECONDS = 0.05


# -- HTTP ------------------------------------------------------------------


@router.get("/events", response_model=EventsResponse, summary="Recent events")
def list_events(
    managed: DatabaseDep,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENT_LIMIT)] = DEFAULT_EVENT_LIMIT,
    category: Annotated[list[EventCategory] | None, Query()] = None,
) -> EventsResponse:
    """Page through retained events by sequence number.

    ``after_seq`` is a stable cursor: sequence numbers are assigned
    monotonically by the tracer, so paging cannot skip or repeat an event even
    while new ones arrive.
    """
    sink = managed.events
    # snapshot() copies under the sink's lock, so the events and the stats
    # below describe one consistent instant.
    records = sink.snapshot(after_seq=after_seq, limit=limit, categories=category)
    stats = sink.stats
    remaining = sink.snapshot(
        after_seq=records[-1].seq if records else after_seq, limit=1, categories=category
    )

    return EventsResponse(
        events=[mappers.trace_record_to_api(item) for item in records],
        stats=mappers.trace_stats_to_api(stats, managed.tracer.level),
        page=PageInfo(
            after_seq=after_seq,
            returned=len(records),
            next_cursor=records[-1].seq if records else None,
            has_more=bool(remaining),
        ),
    )


@router.delete(
    "/events", response_model=TraceLevelResponse, summary="Clear retained events"
)
def clear_events(managed: DatabaseDep) -> TraceLevelResponse:
    managed.events.clear()
    return TraceLevelResponse(
        level=managed.tracer.level.name,  # type: ignore[arg-type]
        stats=mappers.trace_stats_to_api(managed.events.stats, managed.tracer.level),
    )


@router.put("/trace", response_model=TraceLevelResponse, summary="Set the trace level")
def set_trace_level(
    payload: SetTraceLevelRequest, managed: DatabaseDep
) -> TraceLevelResponse:
    managed.tracer.level = TraceLevel[payload.level]
    return TraceLevelResponse(
        level=managed.tracer.level.name,  # type: ignore[arg-type]
        stats=mappers.trace_stats_to_api(managed.events.stats, managed.tracer.level),
    )


@router.get("/trace", response_model=TraceLevelResponse, summary="Get the trace level")
def get_trace_level(managed: DatabaseDep) -> TraceLevelResponse:
    return TraceLevelResponse(
        level=managed.tracer.level.name,  # type: ignore[arg-type]
        stats=mappers.trace_stats_to_api(managed.events.stats, managed.tracer.level),
    )


# -- WebSocket -------------------------------------------------------------


class Subscription:
    """A bounded, non-blocking bridge from engine threads to one WebSocket.

    :meth:`offer` runs on the thread that emitted the event and must never
    block, so it hands the record to the event loop with
    ``call_soon_threadsafe`` and returns immediately.
    """

    __slots__ = ("_closed", "_dropped", "_loop", "_queue")

    def __init__(self, loop: asyncio.AbstractEventLoop, max_size: int) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=max_size)
        self._dropped = 0
        self._closed = False

    def offer(self, item: TraceRecord) -> None:
        """Called from an engine thread. Never blocks, never raises."""
        if self._closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, item)
        except RuntimeError:
            # Loop already closed: the connection is gone. Nothing to do.
            self._closed = True

    def _enqueue(self, item: TraceRecord) -> None:
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(item)

    async def drain(self, max_batch: int) -> list[TraceRecord]:
        """Wait for at least one event, then take up to ``max_batch``."""
        first = await self._queue.get()
        batch = [first]
        while len(batch) < max_batch:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    def take_dropped(self) -> int:
        count = self._dropped
        self._dropped = 0
        return count

    @property
    def total_dropped(self) -> int:
        return self._dropped

    def close(self) -> None:
        self._closed = True


@router.websocket("/events/stream")
async def stream_events(websocket: WebSocket, database_id: str) -> None:
    """Push diagnostic events to the visualizer as they happen.

    Lifecycle:

    1. accept, resolve the database (close with 4404 if it does not exist)
    2. send ``hello`` with the current cursor and trace level
    3. subscribe, then loop: send ``events`` batches and ``dropped`` notices
    4. on disconnect or error, always unsubscribe

    Step 4 matters: a subscription left registered would keep a queue alive and
    keep the engine calling into a dead connection on every event.
    """
    await websocket.accept()

    workspace = get_workspace(websocket)  # type: ignore[arg-type]
    config: ConfigDep = websocket.app.state.config
    try:
        managed: ManagedDatabase = workspace.get(database_id)
    except (DatabaseNotFound, InvalidDatabaseId) as exc:
        await websocket.send_json(
            WsErrorMessage(error=type(exc).__name__, message=str(exc)).model_dump()
        )
        await websocket.close(code=4404)
        return

    loop = asyncio.get_running_loop()
    subscription = Subscription(loop, config.websocket_queue_size)
    sink = CallbackSink(subscription.offer)

    stats = managed.events.stats
    await websocket.send_json(
        WsHelloMessage(
            database_id=database_id,
            last_seq=stats.total_recorded,
            trace_level=managed.tracer.level.name,  # type: ignore[arg-type]
            queue_capacity=config.websocket_queue_size,
            server_time_ns=time.time_ns(),
        ).model_dump()
    )

    managed.subscribe(sink)
    try:
        await asyncio.gather(
            _writer(websocket, subscription, config.websocket_batch_size),
            _reader(websocket, managed),
        )
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except RuntimeError:
        # Starlette raises this when writing to an already-closed socket.
        pass
    finally:
        subscription.close()
        managed.unsubscribe(sink)


async def _writer(
    websocket: WebSocket, subscription: Subscription, batch_size: int
) -> None:
    """Forward batches of events until the socket closes."""
    while True:
        batch = await subscription.drain(batch_size)
        dropped = subscription.take_dropped()
        if dropped:
            await websocket.send_json(
                WsDroppedMessage(
                    count=dropped, total_dropped=subscription.total_dropped + dropped
                ).model_dump()
            )
        await websocket.send_json(
            WsEventsMessage(
                events=[mappers.trace_record_to_api(item) for item in batch]
            ).model_dump()
        )


async def _reader(websocket: WebSocket, managed: ManagedDatabase) -> None:
    """Handle client control frames, and notice when the client goes away."""
    while True:
        raw = await websocket.receive_json()
        try:
            message = WsClientMessage.model_validate(raw)
        except ValidationError as exc:
            await websocket.send_json(
                WsErrorMessage(error="InvalidMessage", message=str(exc)).model_dump()
            )
            continue

        if message.type == "set_level" and message.level is not None:
            managed.tracer.level = TraceLevel[message.level]
        # "ping" needs no reply beyond the protocol-level pong Starlette sends.
