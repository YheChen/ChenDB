"""Integration tests for the SQL parse endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

PARSE = f"{API_PREFIX}/databases/demo/parse"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(ServerConfig(workspace=tmp_path / "workspace"))) as c:
        c.post(f"{API_PREFIX}/databases", json={"database_id": "demo"})
        yield c


def parse(client: TestClient, sql: str) -> dict:
    response = client.post(PARSE, json={"sql": sql})
    # Invalid SQL is a *result*, not a failed request: the editor needs the
    # tokens that did scan plus the error position, and a 4xx would discard both.
    assert response.status_code == 200, response.text
    return response.json()


def nodes_by_id(body: dict) -> dict[int, dict]:
    return {node["node_id"]: node for node in body["ast"]["nodes"]}


# -- feature advertisement -------------------------------------------------


def test_health_reports_sql_parsing(client: TestClient):
    body = client.get(f"{API_PREFIX}/health").json()
    assert body["milestone"] >= 2, "parsing shipped in Milestone 2"
    assert body["features"]["sql"] is True


def test_parse_is_separate_from_query(client: TestClient):
    """Parsing and executing stayed distinct endpoints when M3 added /query.

    /parse returns 200 with partial results for invalid SQL; /query returns 422.
    Collapsing them would lose the editor's ability to show a half-typed query.
    """
    bad = {"sql": "SELECT * FROM"}
    assert client.post(PARSE, json=bad).status_code == 200
    assert client.post(f"{API_PREFIX}/databases/demo/query", json=bad).status_code == 422


# -- happy path ------------------------------------------------------------


def test_a_select_returns_tokens_ast_and_statements(client: TestClient):
    body = parse(client, "SELECT name FROM users WHERE age >= 18")

    assert body["ok"] is True
    assert body["error"] is None
    assert body["lexed_ok"] is True
    assert body["token_count"] == len(body["tokens"]) == 9
    assert body["node_count"] == len(body["ast"]["nodes"]) == 7
    assert body["duration_ns"] > 0

    assert len(body["statements"]) == 1
    statement = body["statements"][0]
    assert statement["kind"] == "SelectStatement"
    assert statement["text"] == "SELECT name FROM users WHERE age >= 18"
    assert body["ast"]["root_ids"] == [statement["root_id"]]


def test_tokens_carry_positions_that_slice_back_to_their_lexeme(client: TestClient):
    sql = "SELECT a, b FROM t"
    body = parse(client, sql)
    for token in body["tokens"]:
        if token["type"] == "eof":
            continue
        assert sql[token["start"] : token["end"]] == token["lexeme"]
        assert token["line"] >= 1 and token["column"] >= 1


def test_keyword_tokens_name_their_keyword(client: TestClient):
    body = parse(client, "select * from t")
    keywords = [token["keyword"] for token in body["tokens"] if token["keyword"]]
    assert keywords == ["SELECT", "FROM"]


def test_literal_tokens_carry_decoded_values(client: TestClient):
    body = parse(client, "SELECT * FROM t WHERE a = 1 AND b = 'x' AND c = 1.5")
    values = [
        token["value"] for token in body["tokens"] if token["type"].endswith("literal")
    ]
    assert values == [1, "x", 1.5]


def test_the_ast_is_a_flat_list_addressable_by_node_id(client: TestClient):
    body = parse(client, "SELECT name FROM users WHERE age >= 18")
    nodes = nodes_by_id(body)

    root = nodes[body["ast"]["root_ids"][0]]
    assert root["node_type"] == "SelectStatement"
    # Every child id must resolve, and no node may be its own ancestor.
    for node in body["ast"]["nodes"]:
        for child_id in node["children"]:
            assert child_id in nodes
            assert child_id != node["node_id"]


def test_each_node_carries_the_source_text_it_came_from(client: TestClient):
    sql = "SELECT name FROM users WHERE age >= 18"
    body = parse(client, sql)
    for node in body["ast"]["nodes"]:
        assert node["text"] == sql[node["start"] : node["end"]]


def test_a_parent_span_contains_its_children(client: TestClient):
    body = parse(client, "SELECT a, b FROM t WHERE x = 1 AND y = 2")
    nodes = nodes_by_id(body)
    for node in body["ast"]["nodes"]:
        for child_id in node["children"]:
            child = nodes[child_id]
            assert node["start"] <= child["start"]
            assert child["end"] <= node["end"]


def test_nodes_carry_a_short_label_for_display(client: TestClient):
    body = parse(client, "SELECT age FROM t WHERE age >= 18")
    labels = {node["node_type"]: node["label"] for node in body["ast"]["nodes"]}
    assert labels["BinaryOp"] == ">="
    assert labels["ColumnRef"] == "age"
    assert labels["Literal"] == "18"
    assert labels["TableRef"] == "t"


def test_operator_and_type_attributes_are_readable_strings(client: TestClient):
    body = parse(client, "SELECT * FROM t WHERE a >= 1")
    binary = next(n for n in body["ast"]["nodes"] if n["node_type"] == "BinaryOp")
    assert binary["attributes"]["operator"] == ">="
    literal = next(n for n in body["ast"]["nodes"] if n["node_type"] == "Literal")
    assert literal["attributes"]["data_type"] == "INTEGER"
    assert literal["attributes"]["value"] == 1


def test_create_table_ast(client: TestClient):
    body = parse(
        client,
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL)",
    )
    assert body["ok"]
    assert body["statements"][0]["kind"] == "CreateTableStatement"
    definitions = [
        node for node in body["ast"]["nodes"] if node["node_type"] == "ColumnDefinition"
    ]
    assert [d["attributes"]["name"] for d in definitions] == ["id", "email"]
    assert definitions[0]["attributes"]["constraints"] == ["PRIMARY KEY"]
    assert definitions[1]["attributes"]["data_type"] == "TEXT"


def test_insert_ast(client: TestClient):
    body = parse(client, "INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')")
    assert body["ok"]
    insert = next(n for n in body["ast"]["nodes"] if n["node_type"] == "InsertStatement")
    assert insert["attributes"]["columns"] == ["a", "b"]
    literals = [n for n in body["ast"]["nodes"] if n["node_type"] == "Literal"]
    assert len(literals) == 4


def test_multiple_statements_share_one_flat_node_list(client: TestClient):
    body = parse(client, "CREATE TABLE t (a INT); INSERT INTO t VALUES (1)")
    assert len(body["statements"]) == 2
    assert len(body["ast"]["root_ids"]) == 2
    ids = [node["node_id"] for node in body["ast"]["nodes"]]
    assert len(ids) == len(set(ids)), "node ids must be unique across the script"


def test_an_empty_script_is_valid_and_produces_nothing(client: TestClient):
    body = parse(client, "   -- just a comment\n")
    assert body["ok"] is True
    assert body["statements"] == []
    assert body["ast"]["nodes"] == []
    assert body["token_count"] == 1  # EOF


# -- errors ----------------------------------------------------------------


def test_a_syntax_error_is_positioned_for_an_editor_marker(client: TestClient):
    body = parse(client, "SELECT * FROM")
    assert body["ok"] is False
    error = body["error"]
    assert error["kind"] == "ParseError"
    assert error["start"] == 13
    assert error["line"] == 1
    assert error["column"] == 14
    assert error["end"] > error["start"], "a marker needs a non-empty range"
    assert "table name" in error["message"]
    assert error["found"] == "end of input"


def test_partial_results_survive_a_syntax_error(client: TestClient):
    # The normal state of a query being typed. Going blank on every keystroke
    # would make the editor useless.
    body = parse(client, "SELECT name FROM")
    assert body["ok"] is False
    assert body["token_count"] == 4
    assert body["lexed_ok"] is True
    assert body["statements"] == []


def test_a_lex_error_reports_that_tokenizing_itself_failed(client: TestClient):
    body = parse(client, "SELECT 'unterminated")
    assert body["ok"] is False
    assert body["lexed_ok"] is False
    assert body["tokens"] == []
    assert body["error"]["kind"] == "LexError"
    assert body["error"]["start"] == 7


def test_the_error_lists_what_would_have_been_accepted(client: TestClient):
    body = parse(client, "SELECT * users")
    assert "FROM" in body["error"]["expected"]


def test_unsupported_sql_is_distinguished_from_a_syntax_error(client: TestClient):
    body = parse(client, "SELECT * FROM a LEFT JOIN b ON a.x = b.x")
    assert body["error"]["kind"] == "UnsupportedSqlError"
    assert "LEFT JOIN" in body["error"]["message"]

    body = parse(client, "SELECT FROM FROM")
    assert body["error"]["kind"] == "ParseError"


def test_a_reserved_word_used_as_a_name_suggests_the_fix(client: TestClient):
    body = parse(client, "SELECT * FROM order")
    assert "quote it" in body["error"]["message"]


@pytest.mark.parametrize(
    "sql",
    ["", "   ", "SELECT", "!!!", "'", "((((((", "\x00", "SELECT * FROM t WHERE"],
)
def test_no_input_can_make_the_endpoint_fail(client: TestClient, sql: str):
    response = client.post(PARSE, json={"sql": sql})
    assert response.status_code == 200
    assert isinstance(response.json()["ok"], bool)


def test_an_oversized_script_is_rejected_by_validation(client: TestClient):
    response = client.post(PARSE, json={"sql": "a" * 200_000})
    assert response.status_code == 422


def test_unknown_request_fields_are_rejected(client: TestClient):
    response = client.post(PARSE, json={"sql": "SELECT 1", "dialect": "postgres"})
    assert response.status_code == 422


def test_parsing_an_unknown_database_is_404(client: TestClient):
    response = client.post(
        f"{API_PREFIX}/databases/missing/parse", json={"sql": "SELECT 1"}
    )
    assert response.status_code == 404


# -- diagnostics -----------------------------------------------------------


def test_parser_events_reach_the_shared_timeline(client: TestClient):
    client.put(f"{API_PREFIX}/databases/demo/trace", json={"level": "VERBOSE"})
    client.delete(f"{API_PREFIX}/databases/demo/events")
    parse(client, "SELECT name FROM users WHERE age >= 18")

    events = client.get(
        f"{API_PREFIX}/databases/demo/events?limit=2000&category=parser"
    ).json()["events"]
    kinds = {event["event_type"] for event in events}
    assert {"TokenizedEvent", "TokenEvent", "AstNodeCreatedEvent", "ParsedEvent"} <= kinds
    assert all(event["category"] == "parser" for event in events)


def test_ast_node_events_are_emitted_bottom_up(client: TestClient):
    # Recursive descent completes leaves before the nodes containing them, and
    # the event order shows it.
    client.put(f"{API_PREFIX}/databases/demo/trace", json={"level": "VERBOSE"})
    client.delete(f"{API_PREFIX}/databases/demo/events")
    parse(client, "SELECT a FROM t WHERE b = 1")

    events = client.get(
        f"{API_PREFIX}/databases/demo/events?limit=2000&category=parser"
    ).json()["events"]
    created = [e["event"] for e in events if e["event_type"] == "AstNodeCreatedEvent"]
    assert created[0]["child_count"] == 0, "the first node built is a leaf"
    assert created[-1]["node_type"] == "SelectStatement"


def test_a_parse_error_is_reported_as_an_event(client: TestClient):
    client.put(f"{API_PREFIX}/databases/demo/trace", json={"level": "OPERATOR"})
    client.delete(f"{API_PREFIX}/databases/demo/events")
    parse(client, "SELECT * FROM")

    events = client.get(
        f"{API_PREFIX}/databases/demo/events?limit=200&category=parser"
    ).json()["events"]
    errors = [e for e in events if e["event_type"] == "ParseErrorEvent"]
    assert errors
    assert errors[0]["event"]["column"] == 14


def test_parsing_still_reads_no_pages(client: TestClient):
    """Parsing stayed purely syntactic when Milestone 4 added binding.

    Binding — which does read the catalog — happens at execution time, not parse
    time, so /parse remains free of I/O. That is why the SQL workspace can parse
    on every keystroke.
    """

    def page_reads() -> int:
        return client.get(f"{API_PREFIX}/databases/demo").json()["stats"]["page_reads"]

    # The measurement itself reads pages now — /databases lists the catalog — so
    # calibrate against two back-to-back probes rather than assuming zero.
    baseline = page_reads()
    probe_cost = page_reads() - baseline

    before = page_reads()
    parse(client, "SELECT * FROM users WHERE age > 1")
    after = page_reads()
    assert after - before == probe_cost
