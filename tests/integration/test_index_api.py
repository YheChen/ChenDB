"""Index endpoints, and indexes reached through SQL.

The API's job here is narrow: expose the *real* tree, not a summary of it. So
these tests check the shape the visualizer draws from (a flat node list, real
page ids, real key strings, real sibling links) and that what the API reports
matches what the engine actually did.
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
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, age INTEGER);
"""


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = ServerConfig(workspace=tmp_path / "workspace")
    with TestClient(create_app(config)) as instance:
        instance.post(
            f"{API_PREFIX}/databases", json={"database_id": "demo", "page_size": 256}
        )
        yield instance


def run(client: TestClient, sql: str) -> list[dict]:
    response = client.post(f"{BASE}/query", json={"sql": sql})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    run(client, SETUP)
    rows = ", ".join(f"({n}, 'u{n}@x.com', {n % 40})" for n in range(300))
    run(client, f"INSERT INTO users VALUES {rows};")
    return client


# -- listing ----------------------------------------------------------------


def test_a_fresh_database_has_no_indexes(client: TestClient):
    body = client.get(f"{BASE}/indexes").json()
    assert body["indexes"] == []


def test_an_index_appears_after_create_index(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    body = seeded.get(f"{BASE}/indexes").json()
    # Two, not one: `users` has a primary key, and since Milestone 17 that
    # implies a unique index on it. It is listed like any other because it *is*
    # one: a real B+ tree with real pages, not bookkeeping.
    assert sorted(i["name"] for i in body["indexes"]) == ["users_age", "users_pkey"]

    (index,) = [i for i in body["indexes"] if i["name"] == "users_age"]
    assert index["name"] == "users_age"
    assert index["table_name"] == "users"
    assert index["column_name"] == "age"
    assert index["data_type"] == "INTEGER"
    assert index["unique"] is False
    assert index["entry_count"] == 300
    assert index["height"] >= 2, "300 rows on a 256-byte page must be more than one leaf"
    assert index["page_count"] > 1


def test_listing_can_be_narrowed_to_one_table(seeded: TestClient):
    run(seeded, "CREATE TABLE other (a INTEGER);")
    run(seeded, "CREATE INDEX users_age ON users (age);")
    run(seeded, "CREATE INDEX other_a ON other (a);")

    names = [i["name"] for i in seeded.get(f"{BASE}/indexes?table=users").json()["indexes"]]
    assert sorted(names) == ["users_age", "users_pkey"]
    # `other` has no primary key, so it contributes only the index just made.
    everything = seeded.get(f"{BASE}/indexes").json()["indexes"]
    assert sorted(i["name"] for i in everything) == ["other_a", "users_age", "users_pkey"]


def test_creating_an_index_over_the_api_matches_creating_it_in_sql(seeded: TestClient):
    response = seeded.post(
        f"{BASE}/indexes",
        json={"name": "users_id", "table": "users", "column": "id", "unique": True},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["index"]["unique"] is True
    assert body["index"]["entry_count"] == 300
    assert body["tree"]["height"] == body["index"]["height"]


def test_a_unique_index_over_duplicates_is_rejected_with_a_reason(seeded: TestClient):
    response = seeded.post(
        f"{BASE}/indexes",
        json={"name": "bad", "table": "users", "column": "age", "unique": True},
    )
    assert response.status_code >= 400
    assert "unique" in response.json()["detail"]["message"].lower()


# -- the tree ---------------------------------------------------------------


def test_the_tree_is_a_flat_node_list_with_a_root_id(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    tree = seeded.get(f"{BASE}/indexes/users_age").json()["tree"]

    ids = [node["page_id"] for node in tree["nodes"]]
    assert tree["root_page_id"] in ids
    assert len(ids) == len(set(ids)), "a node must appear once"
    assert tree["truncated"] is False


def test_every_child_page_id_resolves_to_a_node_in_the_list(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    tree = seeded.get(f"{BASE}/indexes/users_age").json()["tree"]
    present = {node["page_id"] for node in tree["nodes"]}
    for node in tree["nodes"]:
        for child in node["children"]:
            assert child in present, f"page {child} is referenced but not sent"


def test_leaves_are_chained_and_internal_nodes_are_not(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    tree = seeded.get(f"{BASE}/indexes/users_age").json()["tree"]
    leaves = [node for node in tree["nodes"] if node["is_leaf"]]
    internal = [node for node in tree["nodes"] if not node["is_leaf"]]

    assert leaves and internal
    assert all(node["next_leaf_id"] is None for node in internal)
    assert all(node["children"] == [] for node in leaves)
    assert all(node["record_ids"] == [] for node in internal)
    # Exactly one leaf ends the chain.
    assert sum(1 for node in leaves if node["next_leaf_id"] is None) == 1


def test_levels_are_numbered_from_the_leaves_up(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    body = seeded.get(f"{BASE}/indexes/users_age").json()
    tree = body["tree"]
    assert all(node["level"] == 0 for node in tree["nodes"] if node["is_leaf"])
    root = next(n for n in tree["nodes"] if n["page_id"] == tree["root_page_id"])
    assert root["level"] == tree["height"] - 1


def test_keys_arrive_rendered_not_as_raw_bytes(seeded: TestClient):
    run(seeded, "CREATE INDEX users_email ON users (email);")
    tree = seeded.get(f"{BASE}/indexes/users_email").json()["tree"]
    leaf = next(node for node in tree["nodes"] if node["is_leaf"])
    assert leaf["keys"], "a populated leaf must have keys"
    assert all(key.startswith("'") for key in leaf["keys"]), leaf["keys"][:3]


def test_every_internal_node_starts_at_minus_infinity(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    tree = seeded.get(f"{BASE}/indexes/users_age").json()["tree"]
    for node in tree["nodes"]:
        if not node["is_leaf"]:
            assert node["keys"][0] == "-∞"


def test_the_node_budget_truncates_rather_than_sending_everything(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    body = seeded.get(f"{BASE}/indexes/users_age?max_nodes=3").json()
    assert len(body["tree"]["nodes"]) == 3
    assert body["tree"]["truncated"] is True


def test_an_unknown_index_is_a_404_that_lists_what_exists(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    response = seeded.get(f"{BASE}/indexes/nope")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "IndexNotFound"
    assert "users_age" in detail["message"]


def test_stats_reflect_the_work_the_tree_has_done(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    stats = seeded.get(f"{BASE}/indexes/users_age").json()["stats"]
    assert stats["inserts"] == 300
    assert stats["splits"] > 0
    assert stats["root_splits"] >= 1


# -- traced search ----------------------------------------------------------


def test_search_reports_the_path_from_root_to_leaf(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    body = seeded.get(f"{BASE}/indexes/users_age/search?value=7").json()

    assert body["found"] is True
    assert len(body["matches"]) == len([n for n in range(300) if n % 40 == 7])
    assert len(body["path"]) == body["height"]
    tree = seeded.get(f"{BASE}/indexes/users_age").json()["tree"]
    assert body["path"][0] == tree["root_page_id"]
    leaves = {node["page_id"] for node in tree["nodes"] if node["is_leaf"]}
    assert body["path"][-1] in leaves


def test_searching_for_a_missing_key_says_so_without_failing(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    body = seeded.get(f"{BASE}/indexes/users_age/search?value=9999").json()
    assert body["found"] is False
    assert body["matches"] == []
    assert len(body["path"]) == body["height"]


def test_a_value_of_the_wrong_type_is_a_422_not_a_crash(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    response = seeded.get(f"{BASE}/indexes/users_age/search?value=notanumber")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "InvalidKey"


def test_text_keys_are_searchable(seeded: TestClient):
    run(seeded, "CREATE INDEX users_email ON users (email);")
    body = seeded.get(f"{BASE}/indexes/users_email/search?value=u42@x.com").json()
    assert body["found"] is True
    assert len(body["matches"]) == 1


# -- indexes through SQL ----------------------------------------------------


def test_a_query_on_an_indexed_column_plans_an_index_scan(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    result = run(seeded, "SELECT id FROM users WHERE age = 7")[0]
    types = [node["operator_type"] for node in result["plan"]["nodes"]]
    assert "IndexScan" in types
    assert "SeqScan" not in types


def test_a_query_on_an_unindexed_column_still_scans(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    result = run(seeded, "SELECT id FROM users WHERE email = 'u1@x.com'")[0]
    types = [node["operator_type"] for node in result["plan"]["nodes"]]
    assert "SeqScan" in types
    assert "IndexScan" not in types


def test_the_index_and_the_scan_return_the_same_rows(seeded: TestClient):
    without = run(seeded, "SELECT id FROM users WHERE age = 7")[0]["rows"]
    run(seeded, "CREATE INDEX users_age ON users (age);")
    with_index = run(seeded, "SELECT id FROM users WHERE age = 7")[0]["rows"]
    assert sorted(with_index) == sorted(without)


def test_a_range_predicate_becomes_a_bounded_index_scan(seeded: TestClient):
    run(seeded, "CREATE INDEX users_age ON users (age);")
    result = run(seeded, "SELECT id FROM users WHERE age >= 10 AND age <= 12")[0]
    scan = next(n for n in result["plan"]["nodes"] if n["operator_type"] == "IndexScan")
    assert ">= 10" in scan["detail"] and "<= 12" in scan["detail"]
    assert result["rows_returned"] == len([n for n in range(300) if 10 <= n % 40 <= 12])


def test_a_partly_indexed_conjunction_keeps_the_rest_as_a_filter(seeded: TestClient):
    # PostgreSQL's "Index Cond" versus "Filter": the index bounds what is read,
    # the filter only discards what was already read.
    run(seeded, "CREATE INDEX users_age ON users (age);")
    result = run(seeded, "SELECT id FROM users WHERE age = 7 AND email = 'u7@x.com'")[0]
    types = [node["operator_type"] for node in result["plan"]["nodes"]]
    assert "IndexScan" in types
    assert "Filter" in types
    assert result["rows_returned"] == 1


def test_index_events_reach_the_event_stream(seeded: TestClient):
    seeded.put(f"{BASE}/trace", json={"level": "STORAGE"})
    run(seeded, "CREATE INDEX users_age ON users (age);")
    seeded.delete(f"{BASE}/events")
    run(seeded, "SELECT id FROM users WHERE age = 7")

    events = seeded.get(f"{BASE}/events?limit=500").json()["events"]
    categories = {event["category"] for event in events}
    assert "index" in categories
    assert any(event["event_type"] == "RangeScanEvent" for event in events)
