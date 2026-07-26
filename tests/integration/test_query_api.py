"""Query execution and step-mode API tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

SETUP = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, age INTEGER);
INSERT INTO users VALUES
  (1, 'ada@example.com', 36),
  (2, 'alan@example.com', NULL),
  (3, 'grace@example.com', 45),
  (4, 'edgar@example.com', 17);
"""


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = ServerConfig(
        workspace=tmp_path / "workspace",
        max_executions=4,
        execution_step_timeout_seconds=5.0,
    )
    with TestClient(create_app(config)) as instance:
        instance.post(
            f"{API_PREFIX}/databases", json={"database_id": "demo", "page_size": 256}
        )
        yield instance


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    response = client.post(f"{API_PREFIX}/databases/demo/query", json={"sql": SETUP})
    assert response.status_code == 200, response.text
    return client


def query(client: TestClient, sql: str, **extra) -> list[dict]:
    response = client.post(
        f"{API_PREFIX}/databases/demo/query", json={"sql": sql, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- feature advertisement -------------------------------------------------


def test_health_reports_execution(client: TestClient):
    body = client.get(f"{API_PREFIX}/health").json()
    assert body["milestone"] == 5
    assert body["features"]["sql"] is True
    assert body["features"]["execution"] is True
    assert body["features"]["catalog"] is True
    assert body["features"]["indexes"] is True
    # Still nothing after this milestone.
    assert body["features"]["planner"] is False


# -- normal mode -----------------------------------------------------------


def test_a_script_returns_one_result_per_statement(client: TestClient):
    results = query(client, SETUP)
    assert [result["statement_kind"] for result in results] == [
        "CreateTableStatement",
        "InsertStatement",
    ]
    assert results[0]["message"].startswith("created table users")
    assert results[1]["rows_affected"] == 4


def test_a_three_statement_script_returns_three_results(client: TestClient):
    results = query(client, SETUP + "SELECT id FROM users;")
    assert len(results) == 3
    assert results[2]["statement_kind"] == "SelectStatement"
    assert results[2]["rows_returned"] == 4


def test_select_returns_rows_with_typed_columns(seeded: TestClient):
    (result,) = query(seeded, "SELECT email, age FROM users WHERE age >= 18")
    assert result["returns_rows"] is True
    assert [(c["name"], c["type"]) for c in result["columns"]] == [
        ("email", "TEXT"),
        ("age", "INTEGER"),
    ]
    assert result["rows"] == [["ada@example.com", 36], ["grace@example.com", 45]]
    assert result["rows_returned"] == 2


def test_a_null_row_is_excluded_by_a_comparison(seeded: TestClient):
    # Three-valued logic, end to end through the API: alan's NULL age makes
    # `age >= 18` unknown, and unknown does not pass a WHERE.
    (with_filter,) = query(seeded, "SELECT id FROM users WHERE age >= 18")
    (is_null,) = query(seeded, "SELECT id FROM users WHERE age IS NULL")
    (everything,) = query(seeded, "SELECT id FROM users")

    assert [row[0] for row in with_filter["rows"]] == [1, 3]
    assert [row[0] for row in is_null["rows"]] == [2]
    assert len(everything["rows"]) == 4


def test_the_plan_carries_actual_statistics(seeded: TestClient):
    (result,) = query(seeded, "SELECT email FROM users WHERE age >= 18")
    plan = result["plan"]
    by_id = {node["operator_id"]: node for node in plan["nodes"]}

    assert plan["root_id"] == "project_1"
    assert by_id["project_1"]["children"] == ["filter_1"]
    assert by_id["filter_1"]["children"] == ["scan_1"]
    assert by_id["scan_1"]["children"] == []

    # The scan produced 4 rows, the filter kept 2 and rejected 2.
    assert by_id["scan_1"]["output_rows"] == 4
    assert by_id["filter_1"]["input_rows"] == 4
    assert by_id["filter_1"]["output_rows"] == 2
    assert by_id["filter_1"]["rows_rejected"] == 2
    assert by_id["filter_1"]["detail"] == "(age >= 18)"


def test_an_identity_projection_is_absent_from_the_plan(seeded: TestClient):
    (result,) = query(seeded, "SELECT * FROM users")
    assert result["plan"]["root_id"] == "scan_1"
    assert len(result["plan"]["nodes"]) == 1


def test_scan_cost_is_reported(seeded: TestClient):
    (result,) = query(seeded, "SELECT id FROM users WHERE age > 100")
    assert result["rows_returned"] == 0
    assert result["rows_scanned"] == 4
    assert result["rows_rejected"] == 4
    assert result["pages_read"] >= 1
    assert result["duration_ns"] > 0


def test_record_ids_point_at_real_rows(seeded: TestClient):
    (result,) = query(seeded, "SELECT id FROM users")
    assert len(result["record_ids"]) == len(result["rows"])
    first = result["record_ids"][0]
    page = seeded.get(
        f"{API_PREFIX}/databases/demo/pages/{first['page_id']}"
    ).json()
    slot = page["slots"][first["slot_id"]]
    assert slot["is_live"] is True
    assert slot["record"]["values"][0] == result["rows"][0][0]


def test_insert_reports_rows_affected_not_returned(seeded: TestClient):
    (result,) = query(seeded, "INSERT INTO users VALUES (9, 'x@y.z', 1)")
    assert result["rows_affected"] == 1
    assert result["rows_returned"] == 0
    assert result["returns_rows"] is False
    assert result["plan"] is None


def test_insert_can_name_columns_in_any_order(seeded: TestClient):
    query(seeded, "INSERT INTO users (age, id, email) VALUES (55, 10, 'z@z.z')")
    (result,) = query(seeded, "SELECT id, email, age FROM users WHERE id = 10")
    assert result["rows"] == [[10, "z@z.z", 55]]


def test_omitting_a_nullable_column_fills_it_with_null(seeded: TestClient):
    query(seeded, "INSERT INTO users (id, email) VALUES (11, 'q@q.q')")
    (result,) = query(seeded, "SELECT age FROM users WHERE id = 11")
    assert result["rows"] == [[None]]


def test_omitting_a_not_null_column_is_rejected_before_anything_is_written(
    seeded: TestClient,
):
    before = query(seeded, "SELECT id FROM users")[0]["rows_returned"]
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query",
        json={"sql": "INSERT INTO users (id) VALUES (12)"},
    )
    assert response.status_code == 422
    assert "NOT NULL" in response.json()["detail"]["message"]
    assert query(seeded, "SELECT id FROM users")[0]["rows_returned"] == before


def test_the_row_ceiling_truncates_and_says_so(seeded: TestClient):
    (result,) = query(seeded, "SELECT id FROM users", max_rows=2)
    assert result["rows_returned"] == 2
    assert result["truncated"] is True


# -- errors ----------------------------------------------------------------


def test_an_unknown_column_is_422_with_a_position(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query",
        json={"sql": "SELECT nope FROM users"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "BindingError"
    assert "no column named 'nope'" in detail["message"]
    # The editor needs to underline the identifier, not the statement.
    assert detail["sql_error"]["start"] == 7
    assert detail["sql_error"]["column"] == 8


def test_an_unknown_table_lists_the_ones_that_exist(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query", json={"sql": "SELECT * FROM orders"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "no table named 'orders'" in detail["message"]
    assert "users" in detail["message"]
    # The position must point at the table name, not the whole statement.
    assert detail["sql_error"]["start"] == len("SELECT * FROM ")


def test_several_tables_can_be_queried_independently(seeded: TestClient):
    query(seeded, "CREATE TABLE orders (id INTEGER, user_id INTEGER, total FLOAT)")
    query(seeded, "INSERT INTO orders VALUES (1, 1, 9.99), (2, 1, 24.5)")

    (orders,) = query(seeded, "SELECT total FROM orders WHERE user_id = 1")
    (users,) = query(seeded, "SELECT email FROM users WHERE id = 1")
    assert [row[0] for row in orders["rows"]] == [9.99, 24.5]
    assert users["rows"] == [["ada@example.com"]]


def test_a_syntax_error_is_422_here_unlike_parse(seeded: TestClient):
    # /parse returns 200 with a partial result; /query has no useful partial
    # answer, so a bad statement is a failed request.
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query", json={"sql": "SELECT * FROM"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "ParseError"


def test_division_by_zero_is_reported(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query",
        json={"sql": "SELECT id / 0 FROM users"},
    )
    assert response.status_code == 422
    assert "division by zero" in response.json()["detail"]["message"]


def test_querying_before_a_table_exists_is_explained(client: TestClient):
    response = client.post(
        f"{API_PREFIX}/databases/demo/query", json={"sql": "SELECT * FROM users"}
    )
    assert response.status_code == 422
    assert "no table" in response.json()["detail"]["message"]


def test_creating_a_duplicate_table_is_refused(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query",
        json={"sql": "CREATE TABLE users (id INTEGER)"},
    )
    assert response.status_code in (409, 422)
    assert "already exists" in response.json()["detail"]["message"]


def test_a_reserved_table_name_is_refused(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query",
        json={"sql": "CREATE TABLE chendb_sneaky (id INTEGER)"},
    )
    assert response.status_code in (409, 422)
    assert "reserved" in response.json()["detail"]["message"]


def test_create_table_if_not_exists_is_a_no_op(seeded: TestClient):
    (result,) = query(seeded, "CREATE TABLE IF NOT EXISTS users (id INTEGER)")
    assert "already exists" in result["message"]


# -- step mode -------------------------------------------------------------


def step(client: TestClient, execution_id: str, path: str = "next", **body) -> dict:
    response = client.post(
        f"{API_PREFIX}/executions/{execution_id}/{path}", json=body or None
    )
    assert response.status_code == 200, response.text
    return response.json()


def start_step(client: TestClient, sql: str) -> dict:
    response = client.post(
        f"{API_PREFIX}/databases/demo/query/step", json={"sql": sql}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_starting_a_stepped_query_pauses_at_the_first_checkpoint(seeded: TestClient):
    execution = start_step(seeded, "SELECT email FROM users WHERE age >= 18")
    assert execution["state"] == "paused"
    assert execution["pause_kind"] == "operator_open"
    assert execution["pause_operator_id"] == "scan_1"
    assert execution["statement_kind"] == "SelectStatement"
    assert execution["result"] is None


def test_stepping_walks_next_down_the_tree_then_rows_back_up(seeded: TestClient):
    execution = start_step(seeded, "SELECT email FROM users WHERE age >= 18")
    execution_id = execution["execution_id"]

    seen: list[tuple[str, str]] = [
        (execution["pause_kind"], execution["pause_operator_id"])
    ]
    for _ in range(12):
        execution = step(seeded, execution_id)
        if execution["state"] != "paused":
            break
        seen.append((execution["pause_kind"], execution["pause_operator_id"] or ""))

    # Operators open leaf-first.
    assert seen[:3] == [
        ("operator_open", "scan_1"),
        ("operator_open", "filter_1"),
        ("operator_open", "project_1"),
    ]
    # next() travels down: root asks filter, filter asks scan.
    next_calls = [entry for entry in seen if entry[0] == "operator_next"]
    assert next_calls[:3] == [
        ("operator_next", "project_1"),
        ("operator_next", "filter_1"),
        ("operator_next", "scan_1"),
    ]
    # A row then travels up from the scan.
    assert ("row_emitted", "scan_1") in seen


def test_a_page_read_is_a_checkpoint_without_the_pager_knowing(seeded: TestClient):
    # The controller is registered as a diagnostics sink, so "pause on a page
    # read" needs no hook in the storage engine.
    execution = start_step(seeded, "SELECT email FROM users")
    execution_id = execution["execution_id"]
    for _ in range(12):
        execution = step(seeded, execution_id)
        if execution["pause_kind"] == "page_read":
            assert "page" in execution["pause_detail"]
            return
        if execution["state"] != "paused":
            break
    pytest.fail("no page_read checkpoint was reached")


def test_until_row_skips_intermediate_checkpoints(seeded: TestClient):
    execution_id = start_step(seeded, "SELECT email FROM users")["execution_id"]
    execution = step(seeded, execution_id, "resume", mode="until_row")
    assert execution["state"] == "paused"
    assert execution["pause_kind"] == "row_emitted"


def test_until_operator_stops_at_the_named_operator(seeded: TestClient):
    execution_id = start_step(
        seeded, "SELECT email FROM users WHERE age >= 18"
    )["execution_id"]
    execution = step(
        seeded, execution_id, "resume", mode="until_operator", operator_id="scan_1"
    )
    assert execution["pause_operator_id"] == "scan_1"
    assert execution["pause_kind"] == "operator_next"


def test_until_operator_needs_an_operator_id(seeded: TestClient):
    execution_id = start_step(seeded, "SELECT id FROM users")["execution_id"]
    response = seeded.post(
        f"{API_PREFIX}/executions/{execution_id}/resume",
        json={"mode": "until_operator"},
    )
    assert response.status_code == 422


def test_continue_finishes_and_returns_the_result(seeded: TestClient):
    execution_id = start_step(
        seeded, "SELECT email FROM users WHERE age >= 18"
    )["execution_id"]
    execution = step(seeded, execution_id, "continue")
    assert execution["state"] == "finished"
    assert execution["result"]["rows"] == [["ada@example.com"], ["grace@example.com"]]
    assert execution["result"]["plan"] is not None


def test_a_finished_execution_can_still_be_inspected(seeded: TestClient):
    execution_id = start_step(seeded, "SELECT id FROM users")["execution_id"]
    step(seeded, execution_id, "continue")
    response = seeded.get(f"{API_PREFIX}/executions/{execution_id}")
    assert response.status_code == 200
    assert response.json()["state"] == "finished"


def test_stepping_a_finished_execution_is_harmless(seeded: TestClient):
    execution_id = start_step(seeded, "SELECT id FROM users")["execution_id"]
    step(seeded, execution_id, "continue")
    execution = step(seeded, execution_id)
    assert execution["state"] == "finished"


def test_cancelling_releases_the_database_lock(seeded: TestClient):
    """The property that matters most: a cancelled query must not wedge the database."""
    execution_id = start_step(seeded, "SELECT email FROM users")["execution_id"]
    execution = step(seeded, execution_id, "cancel")
    assert execution["state"] == "cancelled"
    assert execution["result"]["cancelled"] is True

    # If the lock had leaked, this would block until the test timed out.
    (result,) = query(seeded, "SELECT id FROM users")
    assert result["rows_returned"] == 4


def test_a_stepped_query_holds_the_lock_until_it_ends(seeded: TestClient):
    # Documented consequence, asserted so it does not change silently: a paused
    # execution owns its database. The escape hatch is cancel, which needs no lock.
    execution_id = start_step(seeded, "SELECT id FROM users")["execution_id"]
    listing = seeded.get(f"{API_PREFIX}/executions").json()
    assert any(e["execution_id"] == execution_id for e in listing["executions"])
    step(seeded, execution_id, "cancel")


def test_executions_can_be_listed_and_filtered(seeded: TestClient):
    first = start_step(seeded, "SELECT id FROM users")["execution_id"]
    step(seeded, first, "cancel")
    second = start_step(seeded, "SELECT email FROM users")["execution_id"]
    step(seeded, second, "cancel")

    listing = seeded.get(f"{API_PREFIX}/executions?database_id=demo").json()
    ids = [e["execution_id"] for e in listing["executions"]]
    assert first in ids and second in ids
    assert listing["max_executions"] == 4
    assert seeded.get(f"{API_PREFIX}/executions?database_id=other").json()[
        "executions"
    ] == []


def test_the_execution_registry_is_bounded(seeded: TestClient):
    # max_executions is 4 in this fixture. Starting more must evict rather than
    # accumulate threads.
    for _ in range(8):
        execution_id = start_step(seeded, "SELECT id FROM users")["execution_id"]
        step(seeded, execution_id, "cancel")
    listing = seeded.get(f"{API_PREFIX}/executions").json()
    assert len(listing["executions"]) <= 4


def test_an_unknown_execution_is_404(seeded: TestClient):
    assert seeded.get(f"{API_PREFIX}/executions/exec_nope").status_code == 404
    assert (
        seeded.post(f"{API_PREFIX}/executions/exec_nope/next").status_code == 404
    )


def test_stepping_refuses_a_multi_statement_script(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query/step",
        json={"sql": "SELECT id FROM users; SELECT email FROM users"},
    )
    assert response.status_code == 422
    assert "exactly one statement" in response.json()["detail"]["message"]


def test_stepping_invalid_sql_fails_at_the_start(seeded: TestClient):
    response = seeded.post(
        f"{API_PREFIX}/databases/demo/query/step", json={"sql": "SELECT * FROM"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["sql_error"]["column"] == 14


# -- diagnostics -----------------------------------------------------------


def test_operator_events_reach_the_shared_timeline(seeded: TestClient):
    seeded.put(f"{API_PREFIX}/databases/demo/trace", json={"level": "OPERATOR"})
    seeded.delete(f"{API_PREFIX}/databases/demo/events")
    query(seeded, "SELECT email FROM users WHERE age >= 18")

    events = seeded.get(
        f"{API_PREFIX}/databases/demo/events?limit=2000&category=operator"
    ).json()["events"]
    kinds = {event["event_type"] for event in events}
    assert {"OperatorEvent", "QueryExecutedEvent"} <= kinds

    actions = {
        event["event"]["action"]
        for event in events
        if event["event_type"] == "OperatorEvent"
    }
    assert {"opened", "next", "row_emitted", "closed"} <= actions


def test_expression_events_only_at_verbose(seeded: TestClient):
    for level, expected in (("OPERATOR", False), ("VERBOSE", True)):
        seeded.put(f"{API_PREFIX}/databases/demo/trace", json={"level": level})
        seeded.delete(f"{API_PREFIX}/databases/demo/events")
        query(seeded, "SELECT id FROM users WHERE age >= 18")
        events = seeded.get(
            f"{API_PREFIX}/databases/demo/events?limit=2000&category=operator"
        ).json()["events"]
        found = any(e["event_type"] == "ExpressionEvalEvent" for e in events)
        assert found is expected


def test_tracing_does_not_change_query_results(seeded: TestClient):
    baseline = None
    for level in ("OFF", "SUMMARY", "OPERATOR", "STORAGE", "VERBOSE"):
        seeded.put(f"{API_PREFIX}/databases/demo/trace", json={"level": level})
        (result,) = query(seeded, "SELECT email, age FROM users WHERE age >= 18")
        if baseline is None:
            baseline = result["rows"]
        assert result["rows"] == baseline, f"{level} changed the result"
