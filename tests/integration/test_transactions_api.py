"""Transactions over HTTP.

The panel these endpoints feed shows a user something they cannot see any other
way: the *cost of being able to change your mind*. So the tests care about the
undo log's size as much as about the rollback working, and they check that the
number reported is the page count rather than the row count.

Both routes into a transaction are exercised (the three POST endpoints and the
three SQL statements) because the explorer uses the buttons and the SQL console
interchangeably, and they must agree.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

BASE = f"{API_PREFIX}/databases/demo"
TXN = f"{BASE}/transactions"

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
    """The query endpoint returns one result per statement."""
    response = client.post(f"{BASE}/query", json={"sql": sql})
    assert response.status_code == 200, response.text
    return response.json()


def state(client: TestClient) -> dict:
    response = client.get(TXN)
    assert response.status_code == 200, response.text
    return response.json()


def rows(client: TestClient) -> list:
    (result,) = run(client, "SELECT id FROM users;")
    return result["rows"]


def table_names(client: TestClient) -> list[str]:
    response = client.get(f"{BASE}/tables")
    assert response.status_code == 200, response.text
    return [table["name"] for table in response.json()]


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    run(client, SETUP)
    run(client, "INSERT INTO users VALUES (1, 'a'), (2, 'b'), (3, 'c');")
    return client


# -- reading the timeline ---------------------------------------------------


def test_an_idle_database_reports_no_active_transaction(seeded: TestClient):
    body = state(seeded)
    assert body["active"] is None
    assert body["in_transaction"] is False
    assert body["in_explicit_transaction"] is False
    assert body["undo_bytes"] == 0


def test_implicit_transactions_appear_in_the_history(seeded: TestClient):
    # Two statements ran, each in an implicit transaction of its own.
    body = state(seeded)
    assert len(body["history"]) >= 2
    assert all(item["implicit"] for item in body["history"])
    assert all(item["state"] == "committed" for item in body["history"])


def test_the_history_is_capped(seeded: TestClient):
    limit = state(seeded)["history_limit"]
    for i in range(limit + 20):
        run(seeded, f"INSERT INTO users VALUES ({100 + i}, 'x');")
    assert len(state(seeded)["history"]) == limit


# -- the three verbs --------------------------------------------------------


def test_begin_opens_an_explicit_transaction(seeded: TestClient):
    response = seeded.post(TXN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "begin"
    assert body["transaction"]["implicit"] is False
    assert body["transaction"]["state"] == "active"

    live = state(seeded)
    assert live["in_transaction"] is True
    assert live["in_explicit_transaction"] is True


def test_rollback_undoes_the_writes(seeded: TestClient):
    before = rows(seeded)
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (40, 'd'), (41, 'e');")
    assert len(rows(seeded)) == len(before) + 2

    response = seeded.post(f"{TXN}/rollback")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "rollback"
    assert body["transaction"]["state"] == "aborted"
    assert body["transaction"]["pages_restored"] >= 1
    assert "page(s) restored" in body["message"]

    assert rows(seeded) == before


def test_commit_keeps_the_writes(seeded: TestClient):
    before = len(rows(seeded))
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (50, 'f');")
    response = seeded.post(f"{TXN}/commit")
    assert response.status_code == 200, response.text
    assert response.json()["transaction"]["state"] == "committed"
    assert len(rows(seeded)) == before + 1


def test_a_finished_transaction_holds_no_undo(seeded: TestClient):
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (60, 'g');")
    committed = seeded.post(f"{TXN}/commit").json()["transaction"]
    assert committed["pages_held"] == 0
    assert committed["undo_bytes"] == 0


# -- the undo log itself ----------------------------------------------------


def test_the_active_transaction_exposes_its_before_images(seeded: TestClient):
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (70, 'h');")

    active = state(seeded)["active"]
    assert active["records"], "an active transaction shows what it is holding"
    record = active["records"][0]
    assert record["before_image_size"] == 512
    assert record["reason"]
    assert active["pages_held"] == len(active["records"])
    assert active["undo_bytes"] == active["pages_held"] * 512


def test_the_undo_log_grows_with_pages_not_rows(seeded: TestClient):
    """The whole point of first-write-wins, visible over HTTP.

    Twenty rows into a fresh table land on very few pages, so the undo log must
    be far smaller than the row count, otherwise the engine would be keeping a
    before-image per write, which is the naive design this one rejects.
    """
    seeded.post(TXN)
    for i in range(20):
        run(seeded, f"INSERT INTO users VALUES ({200 + i}, 'x');")

    active = state(seeded)["active"]
    assert active["pages_written"] > active["pages_held"], (
        "repeat writes to a page must not each cost a before-image"
    )
    assert active["pages_held"] < 20


def test_history_entries_do_not_carry_records(seeded: TestClient):
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (80, 'i');")
    seeded.post(f"{TXN}/commit")
    assert all(item["records"] == [] for item in state(seeded)["history"])


# -- errors -----------------------------------------------------------------


def test_beginning_twice_is_an_error_not_a_nested_transaction(seeded: TestClient):
    assert seeded.post(TXN).status_code == 200
    response = seeded.post(TXN)
    assert response.status_code == 422
    assert "savepoint" in response.json()["message"]


def test_a_failed_transaction_is_reported_as_failed(seeded: TestClient):
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (95, 'l');")
    assert (
        seeded.post(f"{BASE}/query", json={"sql": "SELECT * FROM nope;"}).status_code == 422
    )

    body = state(seeded)
    assert body["is_failed"] is True
    assert body["active"]["state"] == "failed"
    assert body["in_transaction"] is True


def test_committing_a_failed_transaction_rolls_it_back(seeded: TestClient):
    before = rows(seeded)
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (96, 'm');")
    seeded.post(f"{BASE}/query", json={"sql": "SELECT * FROM nope;"})

    response = seeded.post(f"{TXN}/commit")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transaction"]["state"] == "aborted"
    # The outcome, not the request. A client switching on `action` must not be
    # told "commit" for work that was thrown away.
    assert body["action"] == "rollback"
    assert "rolled back" in body["message"]
    assert rows(seeded) == before


def test_committing_nothing_is_an_error(seeded: TestClient):
    response = seeded.post(f"{TXN}/commit")
    assert response.status_code == 422
    assert response.json()["error"] == "TransactionError"


def test_rolling_back_nothing_is_an_error(seeded: TestClient):
    assert seeded.post(f"{TXN}/rollback").status_code == 422


def test_an_unknown_database_is_a_404(client: TestClient):
    assert client.get(f"{API_PREFIX}/databases/nope/transactions").status_code == 404


def test_a_traversal_id_is_rejected_before_the_filesystem(client: TestClient):
    response = client.get(f"{API_PREFIX}/databases/..%2F..%2Fetc/transactions")
    assert response.status_code in (400, 404, 422)


# -- the two routes agree ---------------------------------------------------


def test_sql_and_the_endpoints_drive_the_same_transaction(seeded: TestClient):
    before = rows(seeded)
    run(seeded, "BEGIN;")
    assert state(seeded)["in_explicit_transaction"] is True

    run(seeded, "INSERT INTO users VALUES (90, 'j');")
    # Opened with SQL, closed with a button.
    assert seeded.post(f"{TXN}/rollback").status_code == 200
    assert rows(seeded) == before


def test_a_transaction_opened_by_a_button_is_closed_by_sql(seeded: TestClient):
    before = rows(seeded)
    seeded.post(TXN)
    run(seeded, "INSERT INTO users VALUES (91, 'k');")
    run(seeded, "ROLLBACK;")
    assert rows(seeded) == before
    assert state(seeded)["in_transaction"] is False


def test_create_table_rolls_back_whole(seeded: TestClient):
    """Atomic DDL, which the undo log gets for free by working in pages."""
    seeded.post(TXN)
    run(seeded, "CREATE TABLE gone (id INTEGER PRIMARY KEY);")
    assert "gone" in table_names(seeded)

    seeded.post(f"{TXN}/rollback")
    assert "gone" not in table_names(seeded)
