"""Two consoles, one database, over HTTP.

The point of the session query parameter is that it turns one stateless API
into two independent clients of one engine. These tests are that, end to end:
``?session=alice`` and ``?session=bob`` open separate transactions, see
different data, and get in each other's way — which is what the explorer's
two-console view is showing.
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


def run(client: TestClient, sql: str, session: str = "default") -> list[dict]:
    response = client.post(f"{BASE}/query", json={"sql": sql}, params={"session": session})
    assert response.status_code == 200, response.text
    return response.json()


def ids(client: TestClient, session: str = "default") -> list[int]:
    (result,) = run(client, "SELECT id FROM users;", session)
    return sorted(row[0] for row in result["rows"])


def sessions(client: TestClient) -> dict:
    response = client.get(f"{BASE}/sessions")
    assert response.status_code == 200, response.text
    return response.json()


def locks(client: TestClient) -> dict:
    response = client.get(f"{BASE}/locks")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    run(client, SETUP)
    run(client, "INSERT INTO users VALUES (1, 'a'), (2, 'b'), (3, 'c');")
    return client


# -- two consoles -----------------------------------------------------------


def test_two_sessions_get_two_transactions(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "alice"})
    seeded.post(f"{BASE}/transactions", params={"session": "bob"})

    body = sessions(seeded)
    by_name = {s["session"]: s for s in body["sessions"]}
    assert {"alice", "bob"} <= set(by_name)
    assert by_name["alice"]["transaction_id"] != by_name["bob"]["transaction_id"]


def test_one_session_does_not_see_the_others_uncommitted_rows(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "bob"})
    run(seeded, "INSERT INTO users VALUES (99, 'bob');", "bob")

    assert 99 in ids(seeded, "bob"), "bob sees his own write"
    assert 99 not in ids(seeded, "alice"), "alice does not"

    seeded.post(f"{BASE}/transactions/commit", params={"session": "bob"})
    assert 99 in ids(seeded, "alice"), "…until he commits"


def test_a_reader_is_never_blocked(seeded: TestClient):
    """The claim MVCC exists to make, over HTTP.

    Bob holds row locks. Alice reads anyway, immediately — no wait, no timeout.
    """
    seeded.post(f"{BASE}/transactions", params={"session": "bob"})
    run(seeded, "INSERT INTO users VALUES (100, 'held');", "bob")
    assert locks(seeded)["entries"], "bob is holding something"

    assert ids(seeded, "alice") == [1, 2, 3]
    assert locks(seeded)["readers_blocked"] == 0


def test_the_transaction_view_is_per_session(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "bob"})

    bob = seeded.get(f"{BASE}/transactions", params={"session": "bob"}).json()
    alice = seeded.get(f"{BASE}/transactions", params={"session": "alice"}).json()
    assert bob["in_transaction"] is True
    assert alice["in_transaction"] is False


def test_a_session_name_that_is_not_a_plain_name_is_rejected(seeded: TestClient):
    response = seeded.post(f"{BASE}/transactions", params={"session": "../etc"})
    assert response.status_code == 422


# -- the lock table ---------------------------------------------------------


def test_the_lock_table_reports_holders(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "bob"})
    run(seeded, "INSERT INTO users VALUES (200, 'x');", "bob")

    body = locks(seeded)
    assert body["entries"]
    entry = body["entries"][0]
    assert entry["resource"].startswith("users:")
    assert entry["holders"]
    assert all(mode == "exclusive" for mode in entry["holders"].values())
    assert body["stats"]["granted"] > 0


def test_locks_are_gone_after_a_commit(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "bob"})
    run(seeded, "INSERT INTO users VALUES (300, 'x');", "bob")
    assert locks(seeded)["entries"]

    seeded.post(f"{BASE}/transactions/commit", params={"session": "bob"})
    assert locks(seeded)["entries"] == []


def test_the_wait_for_graph_is_empty_when_nobody_waits(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "bob"})
    run(seeded, "INSERT INTO users VALUES (400, 'x');", "bob")
    assert locks(seeded)["wait_for"] == []


# -- snapshots and isolation ------------------------------------------------


def test_a_session_reports_its_snapshot(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "alice"})
    ids(seeded, "alice")

    alice = next(s for s in sessions(seeded)["sessions"] if s["session"] == "alice")
    assert alice["isolation_level"] == "read committed"
    assert alice["snapshot"] is not None
    assert "xmin=" in alice["snapshot"]
    assert alice["snapshots_taken"] >= 1


def test_read_committed_re_snapshots_per_statement(seeded: TestClient):
    seeded.post(f"{BASE}/transactions", params={"session": "alice"})
    for _ in range(3):
        ids(seeded, "alice")

    alice = next(s for s in sessions(seeded)["sessions"] if s["session"] == "alice")
    assert alice["snapshots_taken"] >= 3


def test_the_horizons_are_reported(seeded: TestClient):
    body = sessions(seeded)
    assert body["next_xid"] > 0
    assert body["oldest_snapshot_xmin"] > 0
    assert body["frozen_xid"] >= 0


# -- versions and vacuum ----------------------------------------------------


def test_a_deleted_row_leaves_a_version_behind(seeded: TestClient):
    rows = seeded.get(f"{BASE}/tables/users/records").json()["rows"]
    target = rows[0]["record_id"]
    seeded.delete(f"{BASE}/tables/users/records/{target['page_id']}/{target['slot_id']}")

    assert ids(seeded) == [2, 3], "the reader sees two rows"
    slot = seeded.get(f"{BASE}/pages/{target['page_id']}").json()["slots"][
        target["slot_id"]
    ]
    assert slot["is_live"] is True, "the version is still physically there"
    assert slot["xmax"] > 0, "…and marked deleted"


def test_vacuum_reclaims_and_says_how_much(seeded: TestClient):
    rows = seeded.get(f"{BASE}/tables/users/records").json()["rows"]
    target = rows[0]["record_id"]
    seeded.delete(f"{BASE}/tables/users/records/{target['page_id']}/{target['slot_id']}")

    response = seeded.post(f"{BASE}/vacuum")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bytes_reclaimed"] == 1
    assert "reclaimed" in body["message"]

    slot = seeded.get(f"{BASE}/pages/{target['page_id']}").json()["slots"][
        target["slot_id"]
    ]
    assert slot["is_live"] is False, "now it really is gone"


def test_vacuum_says_so_when_it_can_do_nothing(seeded: TestClient):
    body = seeded.post(f"{BASE}/vacuum").json()
    assert body["bytes_reclaimed"] == 0
    assert "Nothing to reclaim" in body["message"]


# -- feature advertisement --------------------------------------------------


def test_health_reports_mvcc(client: TestClient):
    body = client.get(f"{API_PREFIX}/health").json()
    assert body["milestone"] >= 10, "MVCC shipped in Milestone 10"
    assert body["features"]["mvcc"] is True
