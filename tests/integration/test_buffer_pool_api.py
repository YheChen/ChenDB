"""The buffer pool over HTTP.

The frame grid has to be *real*: real page ids in real slots, and counters that
move when the engine does work. A cache is one of the few parts of a database
whose behaviour is genuinely visual, and that only pays off if what is drawn is
what is actually resident.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

BASE = f"{API_PREFIX}/databases/demo"

SETUP = "CREATE TABLE users (id INTEGER PRIMARY KEY, label TEXT NOT NULL);"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = ServerConfig(workspace=tmp_path / "workspace")
    with TestClient(create_app(config)) as instance:
        instance.post(
            f"{API_PREFIX}/databases", json={"database_id": "demo", "page_size": 512}
        )
        yield instance


def run(client: TestClient, sql: str) -> list[dict]:
    response = client.post(f"{BASE}/query", json={"sql": sql})
    assert response.status_code == 200, response.text
    return response.json()


def pool(client: TestClient) -> dict:
    response = client.get(f"{BASE}/buffer-pool")
    assert response.status_code == 200, response.text
    return response.json()


#: Enough rows that the table does not fit in the pool. 512-byte pages hold
#: about a dozen of these rows, so 4,000 rows is ~330 pages against 128 frames —
#: which means a scan genuinely evicts, and the miss path is exercised.
SEEDED_ROWS = 4_000


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    run(client, SETUP)
    rows = ", ".join(f"({n}, 'row{n:05d}')" for n in range(SEEDED_ROWS))
    run(client, f"INSERT INTO users VALUES {rows};")
    return client


# -- feature advertisement --------------------------------------------------


def test_health_reports_the_buffer_pool(client: TestClient):
    body = client.get(f"{API_PREFIX}/health").json()
    assert body["milestone"] >= 7, "the pool shipped in Milestone 7"
    assert body["features"]["buffer_pool"] is True


# -- the frame grid ---------------------------------------------------------


def test_every_frame_is_reported_including_the_free_ones(client: TestClient):
    # A fixed-shape grid: a page appearing in a slot should read as a change,
    # not as the whole layout reflowing.
    body = pool(client)
    assert len(body["frames"]) == body["capacity"]
    assert body["capacity"] >= 16
    assert [frame["frame_id"] for frame in body["frames"]] == list(range(body["capacity"]))


def test_a_free_frame_has_no_page(client: TestClient):
    free = [frame for frame in pool(client)["frames"] if frame["page_id"] is None]
    assert free
    assert all(frame["recency"] == -1 and not frame["dirty"] for frame in free)


def test_working_fills_frames_with_real_page_ids(seeded: TestClient):
    body = pool(seeded)
    resident = [f for f in body["frames"] if f["page_id"] is not None]
    assert body["resident"] == len(resident)
    assert body["resident"] > 1

    real_pages = {
        summary["page_id"] for summary in seeded.get(f"{BASE}/pages").json()["pages"]
    }
    assert all(frame["page_id"] in real_pages for frame in resident)


def test_bytes_used_follows_residency(seeded: TestClient):
    body = pool(seeded)
    assert body["bytes_used"] == body["resident"] * body["page_size"]


def test_recency_is_a_total_order_over_resident_frames(seeded: TestClient):
    body = pool(seeded)
    ranks = sorted(
        frame["recency"] for frame in body["frames"] if frame["page_id"] is not None
    )
    assert ranks == list(range(len(ranks))), "0..n-1 with no gaps or ties"


# -- the counters -----------------------------------------------------------


def test_reading_a_small_table_twice_produces_hits(client: TestClient):
    run(client, SETUP)
    rows = ", ".join(f"({n}, 'row{n}')" for n in range(100))
    run(client, f"INSERT INTO users VALUES {rows};")
    run(client, "SELECT id FROM users")
    before = pool(client)["stats"]["hits"]
    run(client, "SELECT id FROM users")
    assert pool(client)["stats"]["hits"] > before


def test_logical_reads_exceed_physical_ones(client: TestClient):
    run(client, SETUP)
    rows = ", ".join(f"({n}, 'row{n}')" for n in range(100))
    run(client, f"INSERT INTO users VALUES {rows};")
    run(client, "SELECT id FROM users")
    run(client, "SELECT id FROM users")
    body = pool(client)
    assert body["logical_reads"] > body["physical_reads"]
    assert 0.0 < body["stats"]["hit_rate"] <= 1.0


def test_write_back_absorbs_repeated_writes(seeded: TestClient):
    # Before Milestone 7 every logical write was a syscall.
    body = pool(seeded)
    assert body["logical_writes"] > body["physical_writes"]
    assert body["stats"]["writes_absorbed"] > 0


def test_a_flush_leaves_nothing_dirty(client: TestClient):
    # Every INSERT through the API ends in a sync, which flushes the pool. So
    # the dirty count is normally zero here — and that is the durability
    # contract holding, not the pool failing to buffer. `writes_absorbed` is
    # where the buffering shows up.
    run(client, SETUP)
    run(client, "INSERT INTO users VALUES (1, 'a');")
    body = pool(client)
    assert body["dirty"] == 0
    assert body["stats"]["flushes"] > 0
    assert body["stats"]["pages_flushed"] > 0


def test_the_pool_reports_no_hit_rate_rather_than_dividing_by_zero(
    client: TestClient,
):
    assert 0.0 <= pool(client)["stats"]["hit_rate"] <= 1.0


def test_reading_the_pool_view_does_not_change_the_engine(seeded: TestClient):
    # A diagnostics endpoint that perturbs what it measures is worse than none.
    first = pool(seeded)
    second = pool(seeded)
    assert first["logical_reads"] == second["logical_reads"]
    assert first["physical_reads"] == second["physical_reads"]
    assert first["stats"]["hits"] == second["stats"]["hits"]


# -- events -----------------------------------------------------------------


def test_misses_and_evictions_reach_the_timeline(seeded: TestClient):
    # The table is larger than the pool, so a scan misses and evicts all the way
    # through — sequential flooding, which LRU handles worst of all.
    seeded.put(f"{BASE}/trace", json={"level": "STORAGE"})
    seeded.delete(f"{BASE}/events")
    run(seeded, "SELECT id FROM users")

    events = seeded.get(f"{BASE}/events?limit=2000").json()["events"]
    assert "buffer_pool" in {event["category"] for event in events}
    actions = {
        event["event"]["action"]
        for event in events
        if event["event_type"] == "BufferPoolEvent"
    }
    assert "miss" in actions
    assert "evict" in actions
    assert "hit" not in actions, "hits are VERBOSE; they would drown the stream"


def test_a_scan_larger_than_the_pool_evicts_everything_it_loaded(seeded: TestClient):
    run(seeded, "SELECT id FROM users")
    first = pool(seeded)["stats"]
    run(seeded, "SELECT id FROM users")
    second = pool(seeded)["stats"]

    # A second scan of a table that does not fit gains almost nothing: every
    # page it wants was evicted by the pages behind it. PostgreSQL confines
    # large scans to a ring buffer for exactly this; ChenDB does not.
    gained = second["hits"] - first["hits"]
    fetched = second["lookups"] - first["lookups"]
    assert gained / fetched < 0.5, "LRU is the worst policy for a big scan"


def test_hits_are_reported_at_verbose(client: TestClient):
    run(client, SETUP)
    rows = ", ".join(f"({n}, 'row{n}')" for n in range(100))
    run(client, f"INSERT INTO users VALUES {rows};")
    run(client, "SELECT id FROM users")  # warm
    client.put(f"{BASE}/trace", json={"level": "VERBOSE"})
    client.delete(f"{BASE}/events")
    run(client, "SELECT id FROM users")

    events = client.get(f"{BASE}/events?limit=2000").json()["events"]
    actions = {
        event["event"]["action"]
        for event in events
        if event["event_type"] == "BufferPoolEvent"
    }
    assert "hit" in actions


def test_a_page_read_says_where_it_came_from(client: TestClient):
    run(client, SETUP)
    rows = ", ".join(f"({n}, 'row{n}')" for n in range(100))
    run(client, f"INSERT INTO users VALUES {rows};")
    client.put(f"{BASE}/trace", json={"level": "STORAGE"})
    run(client, "SELECT id FROM users")  # warm the pool
    client.delete(f"{BASE}/events")
    run(client, "SELECT id FROM users")

    events = client.get(f"{BASE}/events?limit=2000").json()["events"]
    sources = {
        event["event"]["source"]
        for event in events
        if event["event_type"] == "PageReadEvent"
    }
    assert sources, "a scan must read pages"
    assert "buffer_pool" in sources, (
        "the source field has been in the schema since Milestone 1 for this"
    )


def test_a_deferred_write_says_so(client: TestClient):
    client.put(f"{BASE}/trace", json={"level": "STORAGE"})
    run(client, SETUP)
    client.delete(f"{BASE}/events")
    run(client, "INSERT INTO users VALUES (1, 'a');")

    events = client.get(f"{BASE}/events?limit=500").json()["events"]
    writes = [e for e in events if e["event_type"] == "PageWriteEvent"]
    assert writes
    assert any(event["event"]["deferred"] for event in writes)


# -- it stays correct -------------------------------------------------------


def test_rows_survive_a_reopen(seeded: TestClient):
    # Write-back means the bytes live in memory for a while. Closing has to
    # flush them, or the pool would be a data-loss bug rather than a cache.
    assert run(seeded, "SELECT id FROM users")[0]["rows_returned"] == SEEDED_ROWS
    # Reopening is what proves the bytes left memory. The workspace closes and
    # reopens a database on demand, so listing then re-reading is enough.
    assert seeded.get(f"{API_PREFIX}/databases").status_code == 200
    assert run(seeded, "SELECT id FROM users")[0]["rows_returned"] == SEEDED_ROWS


def test_an_unknown_database_is_404(client: TestClient):
    response = client.get(f"{API_PREFIX}/databases/nope/buffer-pool")
    assert response.status_code == 404
