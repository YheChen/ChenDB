"""The log, checkpoints and the crash button over HTTP.

The crash endpoint is the one worth having tests for, because it is the only
endpoint in the API that destroys anything. What these pin down is that it
destroys exactly what a power cut would and nothing else: committed rows survive
because their commit records were ``fsync``ed, uncommitted ones do not, and the
response reports both sides rather than leaving the caller to infer them.
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


def wal(client: TestClient, **params) -> dict:
    response = client.get(f"{BASE}/wal", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def rows(client: TestClient) -> list:
    (result,) = run(client, "SELECT id FROM users;")
    return result["rows"]


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    run(client, SETUP)
    run(client, "INSERT INTO users VALUES (1, 'a'), (2, 'b'), (3, 'c');")
    return client


# -- reading the log --------------------------------------------------------


def test_the_log_reports_its_records(seeded: TestClient):
    body = wal(seeded)
    assert body["enabled"] is True
    assert body["records"], "writing rows writes records"
    assert body["total_records"] >= len(body["records"])

    record = body["records"][0]
    assert record["record_type"] in ("update", "commit", "abort", "checkpoint")
    assert record["lsn"] >= body["base_lsn"]


def test_page_images_are_not_shipped(seeded: TestClient):
    """A record carries up to two whole pages. Sending them would be megabytes
    of base64 that no panel renders — the sizes are what the reader looks at.
    """
    for record in wal(seeded)["records"]:
        assert "before_image" not in record
        assert "after_image" not in record
        assert record["after_image_size"] in (0, 512)


def test_the_log_does_not_leak_a_host_path(seeded: TestClient):
    path = wal(seeded)["path"]
    assert "/" not in path and "\\" not in path


def test_lsns_only_go_up(seeded: TestClient):
    lsns = [record["lsn"] for record in wal(seeded)["records"]]
    assert lsns == sorted(lsns)


def test_the_record_window_is_bounded_and_says_so(seeded: TestClient):
    for i in range(80):
        run(seeded, f"INSERT INTO users VALUES ({100 + i}, 'x');")
    body = wal(seeded, limit=10)
    assert len(body["records"]) == 10
    assert body["total_records"] > 10, "and the caller can tell it is a window"


def test_coalescing_is_reported(seeded: TestClient):
    """Consecutive writes to the same heap page collapse to one record, and the
    counter is how a reader sees that happening rather than being told.
    """
    for i in range(60):
        run(seeded, f"INSERT INTO users VALUES ({200 + i}, 'y');")
    stats = wal(seeded)["stats"]
    assert stats["records_coalesced"] > 0


def test_committing_syncs_the_log(seeded: TestClient):
    before = wal(seeded)["stats"]["syncs"]
    run(seeded, "INSERT INTO users VALUES (300, 'z');")
    after = wal(seeded)["stats"]
    assert after["syncs"] > before, "a commit that is still in memory is not a commit"
    assert after["mean_sync_ns"] > 0


# -- checkpoint -------------------------------------------------------------


def test_a_checkpoint_discards_the_log(seeded: TestClient):
    assert wal(seeded)["size_bytes"] > 0

    response = seeded.post(f"{BASE}/checkpoint")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["log_size_before"] > 0
    assert body["log_size_after"] == 0
    assert "discarded" in body["message"]

    assert wal(seeded)["size_bytes"] == 0
    assert len(rows(seeded)) == 3, "and nothing was lost"


def test_the_base_lsn_moves_past_the_checkpoint(seeded: TestClient):
    before = wal(seeded)["next_lsn"]
    seeded.post(f"{BASE}/checkpoint")
    assert wal(seeded)["base_lsn"] >= before


def test_a_checkpoint_with_a_transaction_open_is_refused(seeded: TestClient):
    seeded.post(f"{BASE}/transactions")
    run(seeded, "INSERT INTO users VALUES (400, 'w');")

    response = seeded.post(f"{BASE}/checkpoint")
    assert response.status_code == 422
    assert "cannot checkpoint" in response.json()["message"]


# -- recovery ---------------------------------------------------------------


def test_a_healthy_database_reports_no_recovery(seeded: TestClient):
    body = seeded.get(f"{BASE}/recovery").json()
    assert body["ran"] is False
    assert body["summary"] == "clean shutdown; nothing to recover"


# -- the crash button -------------------------------------------------------


def test_a_crash_loses_the_uncommitted_and_keeps_the_committed(seeded: TestClient):
    """The demonstration, asserted.

    The explicit transaction is never committed, so its rows have no commit
    record; the three seeded rows do, because each statement ran in an implicit
    transaction that committed and synced.
    """
    seeded.post(f"{BASE}/transactions")
    run(seeded, "INSERT INTO users VALUES (900, 'doomed');")
    assert len(rows(seeded)) == 4

    response = seeded.post(f"{BASE}/crash")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["rows_before"]["users"] == 4
    assert body["rows_after"]["users"] == 3
    assert body["recovered"]["ran"] is True
    assert body["recovered"]["losers"], "the open transaction never committed"
    assert "did not survive" in body["message"]

    assert [row[0] for row in rows(seeded)] == [1, 2, 3]


def test_a_crash_with_nothing_outstanding_loses_nothing(seeded: TestClient):
    response = seeded.post(f"{BASE}/crash")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows_before"] == body["rows_after"]
    assert "Nothing uncommitted" in body["message"]
    assert len(rows(seeded)) == 3


def test_the_database_is_usable_immediately_after_a_crash(seeded: TestClient):
    seeded.post(f"{BASE}/crash")
    run(seeded, "INSERT INTO users VALUES (500, 'later');")
    assert len(rows(seeded)) == 4
    assert seeded.get(f"{BASE}/wal").status_code == 200


def test_crashing_an_unknown_database_is_a_404(client: TestClient):
    assert client.post(f"{API_PREFIX}/databases/nope/crash").status_code == 404


def test_the_recovery_report_survives_until_the_next_open(seeded: TestClient):
    seeded.post(f"{BASE}/transactions")
    run(seeded, "INSERT INTO users VALUES (901, 'doomed');")
    seeded.post(f"{BASE}/crash")

    body = seeded.get(f"{BASE}/recovery").json()
    assert body["ran"] is True
    assert body["pages_undone"] > 0
    assert "undone" in body["summary"]
    assert set(body["phase_ns"]) == {"analysis", "redo", "undo"}


# -- feature advertisement --------------------------------------------------


def test_health_reports_the_wal(client: TestClient):
    body = client.get(f"{API_PREFIX}/health").json()
    assert body["milestone"] >= 9, "the log shipped in Milestone 9"
    assert body["features"]["wal"] is True
