"""Every demo button in the visualizer, run against a real engine.

This suite exists because two demo buttons shipped broken:

    Milestone  8   INSERT INTO t VALUES (1, 'x')   row has 2 values but 3 columns
    Milestone 10   DELETE FROM t WHERE id = 1      DELETE is not implemented yet

The second sat in the UI for a whole milestone. Nobody noticed, because a demo
button is precisely the code that no test touches and that nobody clicks in the
workspace they happen to be working on — and when it did fail, it failed in a
way that looked like the *engine* misbehaving.

The catalogue lives in ``visualizer/src/lib/demoSql.ts`` because that is where
the app reads it from; running it here would mean maintaining a second copy,
which is the failure mode this is meant to end rather than repeat. Node is asked
to evaluate that module and print its statements as JSON, so what runs below is
the same SQL the buttons produce, not a transcription of it.

The four claims checked, per statement:

* it parses, or it is marked as deliberately not parsing;
* it executes, if a button runs it;
* it fails, if a button runs it *and failing is the demonstration*;
* it is built from the open table's real schema — implicitly, because the
  fixture below deliberately has a shape no hardcoded SQL would match.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.server.app import API_PREFIX, create_app
from engine.server.config import ServerConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
EMITTER = REPO_ROOT / "scripts" / "emit_demo_sql.ts"

#: Rows for the fixture table. Four is enough for a scan to have something to
#: do and few enough that a hundred tiny databases stay fast.
SEED_ROWS = [
    (1, "ada@example.com", 36, True),
    (2, "alan@example.com", None, False),
    (3, "grace@example.com", 45, True),
    (4, "edgar@example.com", 17, False),
]


def _emit() -> dict:
    """Ask Node to evaluate the catalogue module and hand back its statements.

    Node 22.6+ strips TypeScript types natively, so this needs no bundler, no
    transpiler and no new dependency — which matters, because a guard that is
    expensive to run is a guard that gets turned off.
    """
    if shutil.which("node") is None:  # pragma: no cover - CI always has node
        message = "node is not installed; the demo-SQL catalogue cannot be read"
        # A guard that goes quiet is worse than no guard, so CI sets
        # CHENDB_REQUIRE_NODE and turns the skip into a failure. Locally a
        # missing node is a reason to skip, not to block the whole suite.
        if os.environ.get("CHENDB_REQUIRE_NODE"):
            pytest.fail(message)
        pytest.skip(message)

    finished = subprocess.run(
        ["node", str(EMITTER)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if finished.returncode != 0:  # pragma: no cover - a broken catalogue
        pytest.fail(f"{EMITTER.relative_to(REPO_ROOT)} failed:\n{finished.stderr.strip()}")
    return json.loads(finished.stdout)


@pytest.fixture(scope="module")
def catalogue() -> dict:
    return _emit()


@pytest.fixture(scope="module")
def statements(catalogue: dict) -> list[dict]:
    return catalogue["statements"]


def _create_table_sql(table: dict) -> str:
    columns = ", ".join(
        " ".join(
            filter(
                None,
                (
                    column["name"],
                    column["type"],
                    "PRIMARY KEY" if column["primary_key"] else "",
                    "NOT NULL"
                    if not column["nullable"] and not column["primary_key"]
                    else "",
                ),
            )
        )
        for column in table["columns"]
    )
    return f"CREATE TABLE {table['name']} ({columns});"


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = ServerConfig(workspace=tmp_path / "workspace")
    with TestClient(create_app(config)) as instance:
        yield instance


def _fresh(client: TestClient, database_id: str, catalogue: dict, seeded: bool) -> None:
    """A database of its own for one statement, so no demo can affect another."""
    response = client.post(
        f"{API_PREFIX}/databases", json={"database_id": database_id, "page_size": 4096}
    )
    assert response.status_code in (200, 201), response.text
    if not seeded:
        return

    table = catalogue["table"]
    rows = ",\n  ".join(
        "(" + ", ".join(_literal(value) for value in row) + ")" for row in SEED_ROWS
    )
    setup = f"{_create_table_sql(table)}\nINSERT INTO {table['name']} VALUES\n  {rows};"
    response = client.post(
        f"{API_PREFIX}/databases/{database_id}/query", json={"sql": setup}
    )
    assert response.status_code == 200, f"the fixture itself is wrong: {response.text}"


def ids(statements: list[dict]) -> list[str]:
    return [statement["id"] for statement in statements]


# -- the catalogue is not empty ---------------------------------------------


def test_the_catalogue_covers_every_workspace_with_a_demo(statements: list[dict]):
    # A guard that silently stops finding anything is worse than no guard, so
    # this asserts the shape of what it found rather than trusting the count.
    prefixes = {statement["id"].split("/")[0] for statement in statements}
    assert prefixes == {"buffer", "transactions", "wal", "mvcc", "editor", "execution"}
    assert len(statements) >= 20


def test_every_statement_has_an_id_and_some_sql(statements: list[dict]):
    assert len(set(ids(statements))) == len(statements), "ids must be unique"
    for statement in statements:
        assert statement["sql"].strip(), statement["id"]


# -- it parses ---------------------------------------------------------------


def test_every_demo_statement_parses(client: TestClient, catalogue: dict):
    """The assertion that would have caught Milestone 10's `DELETE` button.

    One request for the whole catalogue, because parsing needs no database
    state and this is the cheap half of the guard.
    """
    _fresh(client, "parse", catalogue, seeded=False)
    wrong: list[str] = []

    for statement in catalogue["statements"]:
        response = client.post(
            f"{API_PREFIX}/databases/parse/parse", json={"sql": statement["sql"]}
        )
        assert response.status_code == 200, response.text
        accepted = response.json()["error"] is None
        if accepted is not statement["parses"]:
            verb = "was refused" if statement["parses"] else "was accepted"
            detail = response.json()["error"]
            wrong.append(
                f"  {statement['id']} ({statement['label']}) {verb}"
                + (f": {detail['message']}" if detail else "")
            )

    assert not wrong, "demo SQL does not match what demoSql.ts claims:\n" + "\n".join(wrong)


# -- it runs -----------------------------------------------------------------


def _runnable(statements: list[dict]) -> list[dict]:
    return [s for s in statements if s["runs"] != "skip"]


def test_there_are_statements_worth_running(statements: list[dict]):
    assert len(_runnable(statements)) >= 12


@pytest.mark.parametrize("index", range(64))
def test_every_button_does_what_the_catalogue_says(
    client: TestClient, catalogue: dict, index: int
):
    """The assertion that would have caught Milestone 8's arity bug.

    Each statement gets a database of its own: several of them delete every
    row, one leaves a transaction open on purpose, and one creates a table. A
    shared database would make the outcome depend on collection order.

    Parametrised over a fixed range rather than over the catalogue so that a
    statement disappearing shows up as a *skip* in the report — the count is
    pinned separately above.
    """
    runnable = _runnable(catalogue["statements"])
    if index >= len(runnable):
        pytest.skip("no statement at this index")
    statement = runnable[index]

    database_id = f"demo{index}"
    _fresh(client, database_id, catalogue, seeded=statement["on"] == "seeded")
    response = client.post(
        f"{API_PREFIX}/databases/{database_id}/query", json={"sql": statement["sql"]}
    )

    if statement["runs"] == "ok":
        assert response.status_code == 200, (
            f"{statement['id']} ({statement['label']}) is a button the user can "
            f"press and it fails:\n{response.text}"
        )
    else:
        assert response.status_code != 200, (
            f"{statement['id']} ({statement['label']}) is supposed to fail — that "
            f"is the demonstration — and it succeeded"
        )


# -- it is built from the schema, not from a guess ---------------------------


def test_no_statement_hardcodes_a_column_the_fixture_does_not_have(
    catalogue: dict, statements: list[dict]
):
    """The fixture has four columns and one of them is a BOOLEAN.

    Any demo built by hand rather than from `demoRows.ts` would almost
    certainly assume two or three, and `test_every_button_does_what_the_
    catalogue_says` would catch it. This checks the fixture is still doing its
    job, so that test cannot pass by being easy.
    """
    columns = catalogue["table"]["columns"]
    assert len(columns) == 4, "a two-column fixture would not catch an arity bug"
    assert any(column["type"] == "BOOLEAN" for column in columns)
    assert any(
        not column["nullable"] and not column["primary_key"] for column in columns
    ), "a NOT NULL column is what 'break it half-way' violates"
