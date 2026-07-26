"""The planner over HTTP: estimates, alternatives, and EXPLAIN.

What the API has to carry that Milestone 5's did not: what the planner
*expected*, beside what actually happened, and what it turned down. A plan view
that shows only the chosen operators cannot explain a slow query — the gap
between estimated and actual rows is where the answer almost always is.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

BASE = f"{API_PREFIX}/databases/demo"

SETUP = """
CREATE TABLE users (id INTEGER PRIMARY KEY, bucket INTEGER, label TEXT NOT NULL);
"""


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


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    run(client, SETUP)
    rows = ", ".join(f"({n}, {n % 100}, 'row{n:05d}')" for n in range(2000))
    run(client, f"INSERT INTO users VALUES {rows};")
    run(client, "CREATE INDEX users_bucket ON users (bucket);")
    run(client, "ANALYZE users;")
    return client


def plan_of(client: TestClient, sql: str) -> dict:
    result = run(client, sql)[0]
    assert result["plan"] is not None, sql
    return result["plan"]


# -- feature advertisement --------------------------------------------------


def test_health_reports_the_planner(client: TestClient):
    body = client.get(f"{API_PREFIX}/health").json()
    assert body["milestone"] == 7
    assert body["features"]["planner"] is True
    assert body["features"]["buffer_pool"] is True


# -- estimates beside actuals -----------------------------------------------


def test_every_operator_carries_an_estimate_and_an_actual(seeded: TestClient):
    plan = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")
    for node in plan["nodes"]:
        assert node["estimated_rows"] is not None, node["operator_id"]
        assert node["estimated_cost"] is not None
        assert node["estimated_io_cost"] is not None
        assert node["estimated_cpu_cost"] is not None
        assert node["output_rows"] >= 0


def test_a_good_estimate_is_close_to_the_actual(seeded: TestClient):
    # bucket is uniform over 0..99, which is the case the uniformity assumption
    # is exactly right for. 20 rows expected, 20 rows delivered.
    plan = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")
    scan = next(n for n in plan["nodes"] if "Scan" in n["operator_type"])
    assert scan["estimated_rows"] == pytest.approx(scan["output_rows"], rel=0.25)


def test_io_and_cpu_are_reported_separately(seeded: TestClient):
    # They answer different questions — "would a buffer pool help?" against
    # "would a faster predicate help?" — and a single total hides both.
    plan = plan_of(seeded, "SELECT id FROM users WHERE bucket < 90")
    scan = next(n for n in plan["nodes"] if n["operator_type"] == "SeqScan")
    assert scan["estimated_io_cost"] > 0
    assert scan["estimated_cpu_cost"] > 0


def test_the_cost_is_cumulative_up_the_tree(seeded: TestClient):
    plan = plan_of(seeded, "SELECT id FROM users WHERE bucket < 90")
    by_id = {node["operator_id"]: node for node in plan["nodes"]}
    root = by_id[plan["root_id"]]
    child = by_id[root["children"][0]]
    assert root["estimated_cost"] >= child["estimated_cost"]
    assert plan["estimated_cost"] == pytest.approx(root["estimated_cost"])


# -- alternatives -----------------------------------------------------------


def test_the_rejected_alternative_is_reported_with_a_reason(seeded: TestClient):
    plan = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")
    assert len(plan["alternatives"]) == 2

    chosen = [a for a in plan["alternatives"] if a["chosen"]]
    rejected = [a for a in plan["alternatives"] if not a["chosen"]]
    assert len(chosen) == 1
    assert chosen[0]["access_path"] == "PhysicalIndexScan"
    assert rejected[0]["access_path"] == "PhysicalSeqScan"
    assert "cost of the chosen plan" in rejected[0]["rejected_because"]
    assert chosen[0]["rejected_because"] == ""


def test_the_choice_flips_with_selectivity(seeded: TestClient):
    # The whole milestone, in one test. Milestone 5 chose the index for both.
    selective = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")
    broad = plan_of(seeded, "SELECT id FROM users WHERE bucket < 90")

    assert _chosen(selective)["access_path"] == "PhysicalIndexScan"
    assert _chosen(broad)["access_path"] == "PhysicalSeqScan"


def test_the_chosen_alternative_is_the_cheapest(seeded: TestClient):
    for sql in (
        "SELECT id FROM users WHERE bucket = 5",
        "SELECT id FROM users WHERE bucket < 40",
        "SELECT id FROM users WHERE bucket < 95",
    ):
        plan = plan_of(seeded, sql)
        costs = [a["estimated_cost"] for a in plan["alternatives"]]
        assert _chosen(plan)["estimated_cost"] == min(costs), sql


def test_an_index_alternative_names_its_index(seeded: TestClient):
    plan = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")
    index = next(a for a in plan["alternatives"] if a["access_path"] == "PhysicalIndexScan")
    assert index["index_name"] == "users_bucket"
    assert next(
        a for a in plan["alternatives"] if a["access_path"] == "PhysicalSeqScan"
    )["index_name"] is None


def test_a_query_with_no_index_has_one_alternative(seeded: TestClient):
    plan = plan_of(seeded, "SELECT id FROM users WHERE label = 'row00005'")
    assert len(plan["alternatives"]) == 1
    assert _chosen(plan)["access_path"] == "PhysicalSeqScan"


# -- rewrites and statistics ------------------------------------------------


def test_rewrites_are_reported_only_when_they_fire(seeded: TestClient):
    plain = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")
    folded = plan_of(seeded, "SELECT id FROM users WHERE bucket = 2 + 3")
    assert plain["rewrites"] == []
    assert "fold_constants" in folded["rewrites"]


def test_folding_produces_the_same_plan_as_the_folded_literal(seeded: TestClient):
    plain = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")
    folded = plan_of(seeded, "SELECT id FROM users WHERE bucket = 2 + 3")
    assert _chosen(folded)["access_path"] == _chosen(plain)["access_path"]
    assert folded["estimated_cost"] == pytest.approx(plain["estimated_cost"])


def test_the_plan_reports_which_statistics_it_used(seeded: TestClient):
    stats = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")["statistics"]
    assert stats["table_name"] == "users"
    assert stats["row_count"] == 2000
    assert stats["page_count"] > 1
    assert stats["stale"] is False
    assert stats["gathered_at_ns"] > 0


def test_writing_makes_the_statistics_report_themselves_stale(seeded: TestClient):
    run(seeded, "INSERT INTO users VALUES (99999, 5, 'late');")
    stats = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")["statistics"]
    assert stats["stale"] is True
    assert stats["row_count"] == 2000, "still the old count — that is the point"


def test_analyze_clears_the_stale_flag(seeded: TestClient):
    run(seeded, "INSERT INTO users VALUES (99999, 5, 'late');")
    run(seeded, "ANALYZE users;")
    stats = plan_of(seeded, "SELECT id FROM users WHERE bucket = 5")["statistics"]
    assert stats["stale"] is False
    assert stats["row_count"] == 2001


def test_analyze_reports_what_it_gathered(seeded: TestClient):
    result = run(seeded, "ANALYZE users;")[0]
    assert "analyzed users (2000 rows)" in result["message"]
    assert result["returns_rows"] is False


# -- EXPLAIN ----------------------------------------------------------------


def test_explain_returns_the_plan_as_rows(seeded: TestClient):
    result = run(seeded, "EXPLAIN SELECT id FROM users WHERE bucket = 5")[0]
    assert result["returns_rows"] is True
    assert [column["name"] for column in result["columns"]] == ["QUERY PLAN"]
    text = "\n".join(row[0] for row in result["rows"])
    assert "PhysicalIndexScan" in text
    assert "Alternatives considered" in text
    assert "Statistics: 2000 rows" in text


def test_explain_does_not_run_the_query(seeded: TestClient):
    # A plan is data, not operators, so EXPLAIN can cost a query it never runs.
    # The only reads left are gathering statistics and asking the tree its height.
    run(seeded, "ANALYZE users;")
    explained = run(seeded, "EXPLAIN SELECT id FROM users WHERE bucket < 95")[0]
    executed = run(seeded, "SELECT id FROM users WHERE bucket < 95")[0]
    assert explained["pages_read"] < executed["pages_read"] / 10


def test_explain_analyze_runs_it_and_reports_actuals(seeded: TestClient):
    result = run(seeded, "EXPLAIN ANALYZE SELECT id FROM users WHERE bucket = 5")[0]
    text = "\n".join(row[0] for row in result["rows"])
    assert "actual rows=20" in text
    assert "cost=" in text


def test_explain_shows_a_stale_warning(seeded: TestClient):
    run(seeded, "INSERT INTO users VALUES (99999, 5, 'late');")
    result = run(seeded, "EXPLAIN SELECT id FROM users WHERE bucket = 5")[0]
    text = "\n".join(row[0] for row in result["rows"])
    assert "STALE" in text


def test_explain_refuses_a_statement_with_no_plan(seeded: TestClient):
    response = seeded.post(
        f"{BASE}/query", json={"sql": "EXPLAIN INSERT INTO users VALUES (1, 1, 'x')"}
    )
    assert response.status_code >= 400
    assert "only explain a SELECT" in response.json()["detail"]["message"]


def test_explain_cannot_explain_itself(seeded: TestClient):
    response = seeded.post(
        f"{BASE}/query", json={"sql": "EXPLAIN EXPLAIN SELECT id FROM users"}
    )
    assert response.status_code >= 400


# -- events -----------------------------------------------------------------


def test_planner_events_reach_the_timeline(seeded: TestClient):
    seeded.put(f"{BASE}/trace", json={"level": "OPERATOR"})
    seeded.delete(f"{BASE}/events")
    run(seeded, "SELECT id FROM users WHERE bucket = 5")

    events = seeded.get(f"{BASE}/events?limit=500").json()["events"]
    types = {event["event_type"] for event in events}
    assert "planner" in {event["category"] for event in events}
    assert "LogicalPlanEvent" in types
    assert "PhysicalPlanEvent" in types
    assert "PlanAlternativeEvent" in types


def test_every_alternative_is_reported_as_an_event(seeded: TestClient):
    seeded.put(f"{BASE}/trace", json={"level": "OPERATOR"})
    seeded.delete(f"{BASE}/events")
    run(seeded, "SELECT id FROM users WHERE bucket = 5")

    events = seeded.get(f"{BASE}/events?limit=500").json()["events"]
    alternatives = [e for e in events if e["event_type"] == "PlanAlternativeEvent"]
    assert len(alternatives) == 2
    assert sum(1 for e in alternatives if e["event"]["chosen"]) == 1


def test_cost_estimates_are_verbose_only(seeded: TestClient):
    for level, expected in (("OPERATOR", False), ("VERBOSE", True)):
        seeded.put(f"{BASE}/trace", json={"level": level})
        seeded.delete(f"{BASE}/events")
        run(seeded, "SELECT id FROM users WHERE bucket = 5")
        events = seeded.get(f"{BASE}/events?limit=1000").json()["events"]
        present = any(e["event_type"] == "CostEstimateEvent" for e in events)
        assert present is expected, level


def test_analyze_emits_a_statistics_event(seeded: TestClient):
    seeded.put(f"{BASE}/trace", json={"level": "SUMMARY"})
    seeded.delete(f"{BASE}/events")
    run(seeded, "ANALYZE users;")
    events = seeded.get(f"{BASE}/events?limit=200").json()["events"]
    gathered = next(e for e in events if e["event_type"] == "StatisticsGatheredEvent")
    assert gathered["event"]["row_count"] == 2000
    assert gathered["event"]["column_count"] == 3


# -- stepping still works ---------------------------------------------------


def test_a_stepped_query_carries_its_estimates(seeded: TestClient):
    response = seeded.post(
        f"{BASE}/query/step", json={"sql": "SELECT id FROM users WHERE bucket = 5"}
    )
    assert response.status_code == 201, response.text
    execution = response.json()
    seeded.post(f"{API_PREFIX}/executions/{execution['execution_id']}/continue")
    detail = seeded.get(f"{API_PREFIX}/executions/{execution['execution_id']}").json()
    assert detail["plan"] is not None
    assert detail["plan"]["alternatives"], "a stepped plan reports its alternatives too"


def test_stepping_starts_at_the_query_not_the_planner(seeded: TestClient):
    # Planning reads pages — gathering statistics scans the whole table — and
    # from Milestone 6 it happens before the first operator opens. Stepping
    # through it would mean the first dozen steps of every query were the
    # planner counting rows.
    response = seeded.post(
        f"{BASE}/query/step", json={"sql": "SELECT id FROM users WHERE bucket = 5"}
    )
    execution = response.json()
    assert execution["state"] == "paused"
    assert execution["pause_kind"] == "operator_open"
    assert execution["pause_operator_id"] == "scan_1"


def _chosen(plan: dict) -> dict:
    return next(a for a in plan["alternatives"] if a["chosen"])
