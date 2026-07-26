"""WebSocket event-stream tests.

The properties that matter are not "does a message arrive" but the failure
modes: a slow client must not block the engine, and a vanished client must not
leak a subscription.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

PAGE_SIZE = 256
STREAM_PATH = f"{API_PREFIX}/databases/demo/events/stream"

COLUMNS = [
    {"name": "id", "type": "INTEGER", "nullable": False},
    {"name": "payload", "type": "TEXT", "nullable": False},
]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = ServerConfig(workspace=tmp_path / "workspace", websocket_queue_size=16)
    with TestClient(create_app(config)) as instance:
        instance.post(
            f"{API_PREFIX}/databases",
            json={"database_id": "demo", "page_size": PAGE_SIZE},
        )
        instance.post(
            f"{API_PREFIX}/databases/demo/tables",
            json={"name": "t", "columns": COLUMNS},
        )
        yield instance


def insert(client: TestClient, count: int, start: int = 0) -> None:
    client.post(
        f"{API_PREFIX}/databases/demo/tables/t/records",
        json={"rows": [[i, f"row-{i:04d}"] for i in range(start, start + count)]},
    )


def drain_events(
    websocket, *, until: int, max_frames: int = 200, of_type: str | None = None
) -> list[dict]:
    """Collect event payloads until enough have arrived or frames run out.

    ``of_type`` counts only events of that type toward ``until``. Needed because
    the events an operation produces are not all the ones a test cares about —
    an insert also emits catalog lookups and a heap scan of ``chendb_indexes``,
    so counting raw frames would stop before the interesting ones arrive.

    **``until`` must be below the connection's queue capacity.** A burst of work
    happens entirely inside one request, so every event is offered before the
    event loop next runs and the queue drops all but the newest ``queue_size``.
    Asking for more than that blocks on ``receive_json`` forever — which is
    exactly what happened when Milestone 7's ``BufferPoolEvent`` pushed the
    per-insert event count past the ceiling.
    """
    collected: list[dict] = []
    for _ in range(max_frames):
        message = websocket.receive_json()
        if message["type"] == "events":
            collected.extend(message["events"])
        matching = (
            collected
            if of_type is None
            else [event for event in collected if event["event_type"] == of_type]
        )
        if len(matching) >= until:
            break
    return collected


def test_hello_is_the_first_frame(client: TestClient):
    with client.websocket_connect(STREAM_PATH) as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        assert hello["database_id"] == "demo"
        assert hello["trace_level"] == "STORAGE"
        assert hello["queue_capacity"] == 16
        assert hello["last_seq"] >= 0
        assert hello["server_time_ns"] > 0


def test_connecting_to_an_unknown_database_closes_with_4404(client: TestClient):
    with client.websocket_connect(
        f"{API_PREFIX}/databases/missing/events/stream"
    ) as websocket:
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["error"] == "DatabaseNotFound"


def test_events_stream_live_as_the_engine_works(client: TestClient):
    with client.websocket_connect(STREAM_PATH) as websocket:
        assert websocket.receive_json()["type"] == "hello"

        insert(client, 3)
        events = drain_events(websocket, until=3, of_type="RecordInsertedEvent")

    assert events
    types = {event["event_type"] for event in events}
    assert "RecordInsertedEvent" in types

    inserted = next(e for e in events if e["event_type"] == "RecordInsertedEvent")
    assert inserted["category"] == "record"
    assert inserted["event"]["length"] > 0
    assert inserted["seq"] > 0


def test_sequence_numbers_increase_across_frames(client: TestClient):
    # Two batches with a drain between, so the client keeps up and genuinely
    # sees several frames rather than one over-full one. See drain_events on
    # why a single big burst cannot deliver more than the queue capacity.
    seqs: list[int] = []
    with client.websocket_connect(STREAM_PATH) as websocket:
        websocket.receive_json()
        for batch in range(2):
            insert(client, 4, start=batch * 4)
            seqs.extend(
                event["seq"] for event in drain_events(websocket, until=8)
            )

    assert len(seqs) >= 8
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


def test_the_client_can_change_the_trace_level_over_the_socket(client: TestClient):
    with client.websocket_connect(STREAM_PATH) as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "set_level", "level": "OFF"})
        # Round-trip through HTTP to be sure the change was applied.
        insert(client, 1)
        assert client.get(f"{API_PREFIX}/databases/demo/trace").json()["level"] == "OFF"


def test_an_invalid_client_message_gets_an_error_not_a_disconnect(client: TestClient):
    with client.websocket_connect(STREAM_PATH) as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "nonsense"})
        message = websocket.receive_json()
        while message["type"] == "events":
            message = websocket.receive_json()
        assert message["type"] == "error"
        assert message["error"] == "InvalidMessage"


def test_a_client_that_never_reads_cannot_block_the_engine(client: TestClient):
    """The engine must outrun a client without ever waiting for it.

    The connection is opened and then deliberately ignored while a large
    ``VERBOSE`` workload runs. Every insert must still complete: nothing in the
    diagnostics path is allowed to apply backpressure to a storage operation.

    The drop *policy* is unit-tested in ``tests/unit/test_ws_backpressure.py``
    instead — ``TestClient``'s transport buffers without limit, so the writer
    drains the connection queue as fast as it fills and the overflow branch is
    never reached here.
    """
    client.put(f"{API_PREFIX}/databases/demo/trace", json={"level": "VERBOSE"})

    with client.websocket_connect(STREAM_PATH) as websocket:
        websocket.receive_json()

        for batch in range(4):
            response = client.post(
                f"{API_PREFIX}/databases/demo/tables/t/records",
                json={
                    "rows": [
                        [batch * 100 + i, f"row-{i}"] for i in range(100)
                    ]
                },
            )
            assert response.status_code == 201, "a silent WebSocket blocked an insert"

    assert client.get(f"{API_PREFIX}/databases/demo/tables/t/records?limit=1000").json()[
        "returned"
    ] == 400


def test_a_disconnected_client_does_not_block_later_queries(client: TestClient):
    with client.websocket_connect(STREAM_PATH) as websocket:
        websocket.receive_json()
        insert(client, 5)

    # Socket closed. Subsequent work must proceed normally.
    for batch in range(3):
        response = client.post(
            f"{API_PREFIX}/databases/demo/tables/t/records",
            json={"rows": [[100 + batch, f"after-{batch}"]]},
        )
        assert response.status_code == 201

    rows = client.get(f"{API_PREFIX}/databases/demo/tables/t/records?limit=1000").json()
    assert rows["returned"] == 8


def test_subscriptions_are_released_on_disconnect(client: TestClient):
    managed = client.app.state.workspace.get("demo")
    assert managed.subscriber_count == 0

    with client.websocket_connect(STREAM_PATH) as websocket:
        websocket.receive_json()
        insert(client, 1)
        drain_events(websocket, until=1)
        assert managed.subscriber_count == 1

    # The finally block in the handler must have unsubscribed.
    insert(client, 1, start=50)
    assert managed.subscriber_count == 0


def test_several_clients_each_receive_the_events(client: TestClient):
    with (
        client.websocket_connect(STREAM_PATH) as first,
        client.websocket_connect(STREAM_PATH) as second,
    ):
        first.receive_json()
        second.receive_json()
        assert client.app.state.workspace.get("demo").subscriber_count == 2

        insert(client, 2)
        first_events = drain_events(first, until=2)
        second_events = drain_events(second, until=2)

    assert first_events and second_events
    assert {e["seq"] for e in first_events} & {e["seq"] for e in second_events}
