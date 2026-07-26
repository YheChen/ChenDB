"""Diagnostic-event API models and the WebSocket protocol.

Event payloads cross the wire as an untyped ``dict`` rather than a discriminated
union of ~40 models.  The reasoning: every event is a flat frozen dataclass, the
``event_type`` field already identifies it, and the frontend renders payloads
generically.  Hand-maintaining a Pydantic model per event would double the work
of adding one and would drift.  ``event_type`` plus ``category`` is the stable
contract; the payload's field names come straight from the dataclass.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from engine.server.schemas.common import ApiModel, PageInfo, RequestModel

__all__ = [
    "EventsResponse",
    "SetTraceLevelRequest",
    "TraceLevelResponse",
    "TraceRecordModel",
    "TraceStatsModel",
    "WsClientMessage",
    "WsDroppedMessage",
    "WsErrorMessage",
    "WsEventsMessage",
    "WsHelloMessage",
]

TraceLevelName = Literal["OFF", "SUMMARY", "OPERATOR", "STORAGE", "VERBOSE"]

EventCategoryName = Literal[
    "lifecycle",
    "storage",
    "record",
    "parser",
    "operator",
    "catalog",
    "index",
    "planner",
    "buffer_pool",
    "transaction",
    "wal",
    "recovery",
    "lock",
    "mvcc",
]


class TraceRecordModel(ApiModel):
    """One diagnostic event with the envelope the tracer stamped on it."""

    seq: int = Field(description="Monotonic per database; also the pagination cursor")
    timestamp_ns: int
    category: EventCategoryName
    level: TraceLevelName
    event_type: str = Field(description="Event class name, e.g. 'PageReadEvent'")
    event: dict[str, Any] = Field(description="Flat payload; fields depend on event_type")


class TraceStatsModel(ApiModel):
    """Retention state, so a client can tell when it has missed events."""

    capacity: int
    size: int
    total_recorded: int
    dropped: int = Field(
        description="Events evicted from the ring buffer before being read"
    )
    level: TraceLevelName


class EventsResponse(ApiModel):
    events: list[TraceRecordModel]
    stats: TraceStatsModel
    page: PageInfo


class SetTraceLevelRequest(RequestModel):
    level: TraceLevelName


class TraceLevelResponse(ApiModel):
    level: TraceLevelName
    stats: TraceStatsModel


# -- WebSocket protocol ----------------------------------------------------
#
# Server -> client:  hello, events, dropped, error
# Client -> server:  set_level, ping
#
# Every frame is a JSON object with a "type" discriminator.


class WsHelloMessage(ApiModel):
    """First frame after the connection opens."""

    type: Literal["hello"] = "hello"
    database_id: str
    last_seq: int = Field(
        description="Highest sequence number already retained; fetch older "
        "events over HTTP if the client needs history"
    )
    trace_level: TraceLevelName
    queue_capacity: int
    server_time_ns: int


class WsEventsMessage(ApiModel):
    """A batch of events. Batched to avoid one frame per page read."""

    type: Literal["events"] = "events"
    events: list[TraceRecordModel]


class WsDroppedMessage(ApiModel):
    """Backpressure notice: this client was too slow and lost events.

    Reported rather than hidden, so the UI can show a gap instead of implying
    it saw everything.
    """

    type: Literal["dropped"] = "dropped"
    count: int
    total_dropped: int


class WsErrorMessage(ApiModel):
    type: Literal["error"] = "error"
    error: str
    message: str


class WsClientMessage(RequestModel):
    """Anything the client may send."""

    type: Literal["set_level", "ping"]
    level: TraceLevelName | None = None
