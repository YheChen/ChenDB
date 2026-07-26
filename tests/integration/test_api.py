"""HTTP API integration tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

PAGE_SIZE = 256

USERS_COLUMNS = [
    {"name": "id", "type": "INTEGER", "nullable": False, "primary_key": True},
    {"name": "email", "type": "TEXT", "nullable": False},
    {"name": "age", "type": "INTEGER"},
]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(ServerConfig(workspace=tmp_path / "workspace"))
    with TestClient(app) as instance:
        yield instance


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    """A client with a ``demo`` database holding a ``users`` table and 3 rows."""
    client.post(f"{API_PREFIX}/databases", json={"database_id": "demo", "page_size": PAGE_SIZE})
    client.post(
        f"{API_PREFIX}/databases/demo/table",
        json={"name": "users", "columns": USERS_COLUMNS},
    )
    client.post(
        f"{API_PREFIX}/databases/demo/records",
        json={"rows": [[1, "ada@example.com", 36], [2, "alan@example.com", None], [3, "grace@example.com", 45]]},
    )
    return client


# -- meta ------------------------------------------------------------------


def test_health_reports_the_milestone_and_feature_flags(client: TestClient):
    body = client.get(f"{API_PREFIX}/health").json()
    assert body["milestone"] == 1
    assert body["api_version"] == "v1"
    # Panels for unbuilt features must be advertised as absent, not stubbed.
    assert body["features"]["storage"] is True
    assert body["features"]["page_inspector"] is True
    assert body["features"]["sql"] is False
    assert body["features"]["mvcc"] is False


def test_health_does_not_leak_an_absolute_path(client: TestClient):
    workspace = client.get(f"{API_PREFIX}/health").json()["workspace"]
    assert "/" not in workspace and "\\" not in workspace


def test_openapi_schema_is_served(client: TestClient):
    spec = client.get(f"{API_PREFIX}/openapi.json").json()
    assert spec["info"]["title"] == "ChenDB Engine API"
    assert f"{API_PREFIX}/databases" in spec["paths"]


def test_endpoints_for_unbuilt_milestones_are_absent_not_stubbed(client: TestClient):
    for path in ("/query", "/executions/1", "/buffer-pool", "/locks", "/wal", "/indexes/x"):
        assert client.get(f"{API_PREFIX}{path}").status_code == 404


# -- lifecycle -------------------------------------------------------------


def test_create_and_list_databases(client: TestClient):
    assert client.get(f"{API_PREFIX}/databases").json()["databases"] == []

    created = client.post(
        f"{API_PREFIX}/databases", json={"database_id": "demo", "page_size": PAGE_SIZE}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["database_id"] == "demo"
    assert body["page_size"] == PAGE_SIZE
    assert body["page_count"] == 1
    assert body["table_name"] is None

    listing = client.get(f"{API_PREFIX}/databases").json()["databases"]
    assert [entry["database_id"] for entry in listing] == ["demo"]
    assert listing[0]["is_open"] is True


def test_creating_a_duplicate_database_conflicts(client: TestClient):
    client.post(f"{API_PREFIX}/databases", json={"database_id": "demo"})
    duplicate = client.post(f"{API_PREFIX}/databases", json={"database_id": "demo"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["error"] == "DatabaseAlreadyExists"


def test_unknown_database_is_404(client: TestClient):
    response = client.get(f"{API_PREFIX}/databases/nope")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "DatabaseNotFound"


def test_delete_removes_the_file(client: TestClient, tmp_path: Path):
    client.post(f"{API_PREFIX}/databases", json={"database_id": "temp"})
    assert client.delete(f"{API_PREFIX}/databases/temp").status_code == 204
    assert client.get(f"{API_PREFIX}/databases").json()["databases"] == []
    assert not (tmp_path / "workspace" / "temp.chendb").exists()


def test_data_survives_reopening_through_the_api(seeded: TestClient):
    # Close the handle by deleting nothing but forcing a fresh open: list then
    # re-fetch. The rows must come back from disk, not from memory.
    seeded.app.state.workspace.close("demo")
    rows = seeded.get(f"{API_PREFIX}/databases/demo/records").json()["rows"]
    assert [row["values"][0] for row in rows] == [1, 2, 3]


# -- security --------------------------------------------------------------


@pytest.mark.parametrize(
    "database_id",
    ["../etc/passwd", "..", "../../secret", "a/b", "a\\b", ".hidden", "-leading", "x" * 65],
)
def test_path_traversal_and_bad_ids_are_rejected(client: TestClient, database_id: str):
    response = client.get(f"{API_PREFIX}/databases/{database_id}")
    assert response.status_code in (404, 422), response.text


@pytest.mark.parametrize("database_id", ["../escape", "a/b"])
def test_traversal_is_rejected_on_create_too(client: TestClient, database_id: str):
    response = client.post(f"{API_PREFIX}/databases", json={"database_id": database_id})
    assert response.status_code == 422


def test_no_response_contains_an_absolute_filesystem_path(seeded: TestClient):
    for path in (
        "/health",
        "/databases",
        "/databases/demo",
        "/databases/demo/pages",
        "/databases/demo/records",
    ):
        body = seeded.get(f"{API_PREFIX}{path}").text
        assert "/private/" not in body and "/tmp/" not in body, path


def test_unknown_request_fields_are_rejected(client: TestClient):
    response = client.post(
        f"{API_PREFIX}/databases", json={"database_id": "demo", "typo_field": 1}
    )
    assert response.status_code == 422


# -- schema ----------------------------------------------------------------


def test_create_table_returns_the_schema(client: TestClient):
    client.post(f"{API_PREFIX}/databases", json={"database_id": "demo", "page_size": PAGE_SIZE})
    response = client.post(
        f"{API_PREFIX}/databases/demo/table",
        json={"name": "users", "columns": USERS_COLUMNS},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "users"
    assert [column["name"] for column in body["schema"]["columns"]] == ["id", "email", "age"]
    assert body["schema"]["null_bitmap_size"] == 1
    assert body["schema"]["fixed_row_size"] is None  # TEXT makes rows variable
    assert body["row_count"] == 0


def test_a_second_table_is_rejected_in_milestone_1(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/table",
        json={"name": "orders", "columns": USERS_COLUMNS},
    )
    assert response.status_code == 409
    assert "Milestone 4" in response.json()["detail"]["message"]


def test_table_endpoint_404s_before_a_table_exists(client: TestClient):
    client.post(f"{API_PREFIX}/databases", json={"database_id": "empty"})
    response = client.get(f"{API_PREFIX}/databases/empty/table")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "NoTable"


def test_an_invalid_column_type_is_rejected(client: TestClient):
    client.post(f"{API_PREFIX}/databases", json={"database_id": "demo"})
    response = client.post(
        f"{API_PREFIX}/databases/demo/table",
        json={"name": "t", "columns": [{"name": "c", "type": "BLOB"}]},
    )
    assert response.status_code == 422


# -- rows ------------------------------------------------------------------


def test_insert_reports_addresses_and_cost(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/records", json={"rows": [[4, "x@y.z", 1]]}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["inserted"] == 1
    assert set(body["record_ids"][0]) == {"page_id", "slot_id"}
    assert body["duration_ns"] > 0


def test_scan_reports_rows_pages_and_time(seeded: TestClient):
    body = seeded.get(f"{API_PREFIX}/databases/demo/records").json()
    assert [row["values"] for row in body["rows"]] == [
        [1, "ada@example.com", 36],
        [2, "alan@example.com", None],
        [3, "grace@example.com", 45],
    ]
    assert body["returned"] == 3
    assert body["rows_scanned"] == 3
    assert body["pages_read"] >= 1
    assert body["has_more"] is False
    assert [column["name"] for column in body["columns"]] == ["id", "email", "age"]


def test_scan_paginates(seeded: TestClient):
    first = seeded.get(f"{API_PREFIX}/databases/demo/records?limit=2").json()
    assert first["returned"] == 2 and first["has_more"] is True

    second = seeded.get(f"{API_PREFIX}/databases/demo/records?offset=2&limit=2").json()
    assert second["returned"] == 1 and second["has_more"] is False
    assert second["rows"][0]["values"][0] == 3


def test_type_errors_are_reported_per_column(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/records", json={"rows": [["not-an-int", "a@b.c", 1]]}
    )
    assert response.status_code == 422
    assert "'id'" in response.json()["detail"]["message"]


def test_not_null_violations_are_reported(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/records", json={"rows": [[9, None, 1]]}
    )
    assert response.status_code == 422
    assert "NOT NULL" in response.json()["detail"]["message"]


def test_wrong_arity_is_reported(seeded: TestClient):
    response = seeded.post(f"{API_PREFIX}/databases/demo/records", json={"rows": [[1]]})
    assert response.status_code == 422


def test_delete_by_record_id(seeded: TestClient):
    rows = seeded.get(f"{API_PREFIX}/databases/demo/records").json()["rows"]
    target = rows[1]["record_id"]

    response = seeded.delete(
        f"{API_PREFIX}/databases/demo/records/{target['page_id']}/{target['slot_id']}"
    )
    assert response.status_code == 200 and response.json()["deleted"] is True

    remaining = seeded.get(f"{API_PREFIX}/databases/demo/records").json()
    assert remaining["returned"] == 2

    # Deleting again is a no-op, not an error.
    again = seeded.delete(
        f"{API_PREFIX}/databases/demo/records/{target['page_id']}/{target['slot_id']}"
    )
    assert again.json()["deleted"] is False


# -- pages -----------------------------------------------------------------


def test_page_list_covers_the_whole_file(seeded: TestClient):
    body = seeded.get(f"{API_PREFIX}/databases/demo/pages").json()
    assert [page["page_id"] for page in body["pages"]] == list(range(body["page_count"]))
    assert body["total_bytes"] == body["page_size"] * body["page_count"]

    owners = {page["owner"] for page in body["pages"]}
    assert {"meta", "schema", "users"} <= owners
    assert all(page["checksum_valid"] for page in body["pages"])
    # No buffer pool yet, so nothing can be cached-and-dirty.
    assert all(page["dirty"] is False for page in body["pages"])


def test_meta_page_detail_exposes_the_file_header(seeded: TestClient):
    body = seeded.get(f"{API_PREFIX}/databases/demo/pages/0").json()
    fields = {field["name"]: field for field in body["header_fields"]}
    assert fields["magic"]["value"].startswith("ChenDB")
    assert fields["page_size"]["value"] == PAGE_SIZE
    assert fields["magic"]["raw_hex"].startswith("4368656e4442")  # "ChenDB"
    assert body["summary"]["page_type"] == "META"


def test_heap_page_detail_decodes_slots_records_and_the_null_bitmap(seeded: TestClient):
    pages = seeded.get(f"{API_PREFIX}/databases/demo/pages").json()["pages"]
    heap_page_id = next(page["page_id"] for page in pages if page["owner"] == "users")

    body = seeded.get(f"{API_PREFIX}/databases/demo/pages/{heap_page_id}").json()
    assert body["summary"]["page_type"] == "HEAP"
    assert len(body["raw_hex"]) == PAGE_SIZE * 2

    slots = body["slots"]
    assert len(slots) == 3
    assert slots[0]["record"]["values"] == [1, "ada@example.com", 36]

    # Row 2 has a NULL age: bit 2 set, and the field occupies no bytes.
    nullable_row = slots[1]["record"]
    assert nullable_row["null_bitmap_bits"] == [False, False, True]
    assert nullable_row["fields"][2]["is_null"] is True
    assert nullable_row["fields"][2]["offset"] == -1

    # Regions must tile the page.
    assert body["header_size"] == 24
    assert body["slot_directory_end"] == 24 + 3 * 4 == body["free_start"]
    assert body["free_start"] < body["free_end"] < body["page_size"]


def test_a_deleted_slot_shows_as_a_tombstone(seeded: TestClient):
    rows = seeded.get(f"{API_PREFIX}/databases/demo/records").json()["rows"]
    target = rows[0]["record_id"]
    seeded.delete(
        f"{API_PREFIX}/databases/demo/records/{target['page_id']}/{target['slot_id']}"
    )

    body = seeded.get(f"{API_PREFIX}/databases/demo/pages/{target['page_id']}").json()
    tombstone = body["slots"][target["slot_id"]]
    assert tombstone["is_live"] is False
    assert tombstone["record"] is None
    assert body["summary"]["reclaimable_space"] > 0


def test_page_out_of_range_is_404(seeded: TestClient):
    assert seeded.get(f"{API_PREFIX}/databases/demo/pages/9999").status_code == 404


def test_negative_page_id_is_rejected(seeded: TestClient):
    assert seeded.get(f"{API_PREFIX}/databases/demo/pages/-1").status_code in (400, 422)


# -- diagnostics -----------------------------------------------------------


def test_events_are_recorded_and_paginated(seeded: TestClient):
    body = seeded.get(f"{API_PREFIX}/databases/demo/events?limit=5").json()
    assert len(body["events"]) == 5
    assert [event["seq"] for event in body["events"]] == sorted(
        event["seq"] for event in body["events"]
    )
    assert body["page"]["has_more"] is True

    cursor = body["page"]["next_cursor"]
    following = seeded.get(
        f"{API_PREFIX}/databases/demo/events?after_seq={cursor}&limit=5"
    ).json()
    assert all(event["seq"] > cursor for event in following["events"])


def test_events_can_be_filtered_by_category(seeded: TestClient):
    body = seeded.get(f"{API_PREFIX}/databases/demo/events?category=record").json()
    assert body["events"]
    assert {event["category"] for event in body["events"]} == {"record"}


def test_event_payloads_carry_real_storage_facts(seeded: TestClient):
    events = seeded.get(f"{API_PREFIX}/databases/demo/events?limit=2000").json()["events"]
    reads = [event for event in events if event["event_type"] == "PageReadEvent"]
    assert reads
    for event in reads:
        payload = event["event"]
        assert payload["file_offset"] == payload["page_id"] * PAGE_SIZE
        assert payload["source"] == "disk"


def test_trace_level_can_be_read_and_changed(seeded: TestClient):
    assert seeded.get(f"{API_PREFIX}/databases/demo/trace").json()["level"] == "STORAGE"

    updated = seeded.put(f"{API_PREFIX}/databases/demo/trace", json={"level": "OFF"})
    assert updated.status_code == 200 and updated.json()["level"] == "OFF"

    seeded.events_before = seeded.get(f"{API_PREFIX}/databases/demo/events?limit=2000").json()
    before = seeded.events_before["stats"]["total_recorded"]
    seeded.post(f"{API_PREFIX}/databases/demo/records", json={"rows": [[99, "q@r.s", 1]]})
    after = seeded.get(f"{API_PREFIX}/databases/demo/events?limit=1").json()["stats"]
    assert after["total_recorded"] == before, "OFF must record nothing"


def test_an_invalid_trace_level_is_rejected(seeded: TestClient):
    response = seeded.put(f"{API_PREFIX}/databases/demo/trace", json={"level": "LOUD"})
    assert response.status_code == 422


def test_events_can_be_cleared(seeded: TestClient):
    response = seeded.delete(f"{API_PREFIX}/databases/demo/events")
    assert response.status_code == 200
    assert response.json()["stats"]["size"] == 0
    assert seeded.get(f"{API_PREFIX}/databases/demo/events").json()["events"] == []


def test_diagnostics_snapshots_are_internally_consistent(seeded: TestClient):
    """A page listing must describe one instant, not a smear of several.

    Every page id appears exactly once, and the reported page count matches the
    number of entries — impossible if the response were assembled while the
    file grew underneath it.
    """
    for _ in range(5):
        seeded.post(
            f"{API_PREFIX}/databases/demo/records",
            json={"rows": [[i, f"user{i}@x.y", i] for i in range(20)]},
        )
        body = seeded.get(f"{API_PREFIX}/databases/demo/pages").json()
        page_ids = [page["page_id"] for page in body["pages"]]
        assert len(page_ids) == len(set(page_ids)) == body["page_count"]
        assert body["total_bytes"] == body["page_size"] * body["page_count"]
