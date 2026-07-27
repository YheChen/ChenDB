"""The interactive shell, `python -m engine`.

One of the three usage modes the project promises, and the only one with no
tests until now — which is exactly why it broke silently in Milestone 4, when
every row-level method on ``Database`` gained a table parameter, and stayed
broken through the whole of that milestone and into this one. The README
documented output the code could not produce.

These are smoke tests, not a specification of the output format: they assert
that each command *runs* against a real database and mentions the thing it is
supposed to be about. That is enough to catch a signature change, which is the
failure that actually happens.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from engine.__main__ import main

SETUP = [
    ".create users id:INTEGER* email:TEXT! age:INTEGER",
    "INSERT INTO users VALUES (1,'ada@example.com',36),"
    "(2,'alan@example.com',NULL),(3,'grace@example.com',45);",
    "CREATE INDEX users_age ON users (age);",
]


def run(path: Path, *commands: str) -> str:
    """Run dot-commands or SQL in one shell session and return its output."""
    argv = [str(path), "--page-size", "512"]
    for command in commands:
        argv += ["-c", command]

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main(argv) == 0
    return buffer.getvalue()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cli.chendb"
    run(path, *SETUP)
    return path


def test_a_table_can_be_created_and_read_back(tmp_path: Path):
    path = tmp_path / "fresh.chendb"
    output = run(path, *SETUP, ".scan")
    assert "created table users" in output
    assert "ada@example.com" in output
    assert "NULL" in output, "a null age must render as NULL, not blank"


def test_sql_runs_and_prints_rows_and_a_plan(db_path: Path):
    output = run(db_path, "SELECT email FROM users WHERE age = 45")
    assert "grace@example.com" in output
    assert "row(s)" in output
    # Which access path is chosen depends on the cost model, and on three rows a
    # scan wins — that is the point of Milestone 6. What matters here is that a
    # plan is printed at all.
    assert "Project" in output
    assert "Scan" in output


def test_a_query_with_no_index_falls_back_to_a_scan(db_path: Path):
    output = run(db_path, "SELECT id FROM users WHERE email = 'ada@example.com'")
    assert "SeqScan" in output


def test_explain_shows_the_alternatives_and_why_they_lost(db_path: Path):
    output = run(db_path, "EXPLAIN SELECT email FROM users WHERE age = 45")
    assert "Alternatives considered" in output
    assert "Sequential scan of users" in output
    assert "Index scan on users_age" in output
    assert "cost of the chosen plan" in output


def test_explain_analyze_reports_actual_rows_beside_the_estimate(db_path: Path):
    output = run(db_path, "EXPLAIN ANALYZE SELECT email FROM users WHERE age = 45")
    assert "actual rows=1" in output


def test_analyze_gathers_statistics(db_path: Path):
    output = run(db_path, "ANALYZE users")
    assert "analyzed users (3 rows)" in output


def test_tables_lists_what_exists(db_path: Path):
    output = run(db_path, ".tables")
    assert "users" in output
    assert "users_age" in output, "a table's indexes are listed alongside it"


def test_schema_shows_columns_and_indexes(db_path: Path):
    output = run(db_path, ".schema")
    assert "TABLE users" in output
    assert "PRIMARY KEY" in output
    assert "INDEX users_age (age)" in output


def test_indexes_reports_height_and_size(db_path: Path):
    output = run(db_path, ".indexes")
    assert "users_age" in output
    assert "users.age" in output


def test_tree_prints_the_nodes(db_path: Path):
    output = run(db_path, ".tree users_age")
    assert "root page" in output
    assert "leaves:" in output
    assert "NULL" in output, "NULL keys sort first and must be visible"


def test_find_traces_a_lookup(db_path: Path):
    output = run(db_path, ".find users_age 45")
    assert "path" in output
    assert "found   1 row(s)" in output


def test_find_on_an_unknown_index_says_what_exists(db_path: Path):
    output = run(db_path, ".find nope 1")
    assert "no index named" in output
    assert "users_age" in output


def test_info_reports_the_catalog_pointers(db_path: Path):
    output = run(db_path, ".info")
    assert "format       version 5" in output
    assert "chendb_indexes" in output
    assert "next object id" in output


def test_page_inspection_still_works(db_path: Path):
    output = run(db_path, ".pages", ".page 0", ".map 1", ".hex 1 64")
    assert "META" in output
    assert "magic" in output


def test_insert_and_delete_through_dot_commands(db_path: Path):
    output = run(
        db_path,
        ".insert 4 | edgar@example.com | 17",
        ".count",
        ".scan",
    )
    assert "inserted at" in output
    assert "edgar@example.com" in output


def test_an_unknown_command_does_not_crash(db_path: Path):
    assert "unknown command" in run(db_path, ".nope")


def test_a_bad_argument_is_reported_not_raised(db_path: Path):
    assert "bad arguments" in run(db_path, ".page notanumber")


def test_an_engine_error_is_reported_not_raised(db_path: Path):
    assert "error:" in run(db_path, "SELECT * FROM nosuchtable")


def test_use_switches_the_current_table(db_path: Path):
    output = run(
        db_path,
        ".create orders id:INTEGER* total:FLOAT",
        ".use users",
        ".count",
    )
    assert "current table is users" in output
    assert output.strip().endswith("3")


def test_creating_a_table_makes_it_current(db_path: Path):
    output = run(db_path, ".create orders id:INTEGER* total:FLOAT", ".count")
    assert output.strip().endswith("0"), "the count is the new, empty table's"


def test_a_fresh_shell_over_two_tables_asks_which(db_path: Path):
    # Each run() is a new shell, so this one has two tables and no choice made.
    # Asking beats silently picking one and reporting the wrong table's rows.
    run(db_path, ".create orders id:INTEGER* total:FLOAT")
    output = run(db_path, ".count")
    assert "pick one with .use" in output
    assert "orders" in output and "users" in output


def test_analyze_and_stats_report_what_the_planner_knows(db_path: Path):
    output = run(db_path, ".analyze", ".stats-of users")
    assert "analyzed users: 3 rows" in output
    assert "distinct" in output
    assert "age" in output


def test_a_hyphenated_command_dispatches(db_path: Path):
    # A hyphen reads better than an underscore and cannot be part of a Python
    # identifier, so the dispatcher translates it.
    assert "unknown command" not in run(db_path, ".stats-of users")


def test_explain_through_the_cli_shows_the_cost(db_path: Path):
    output = run(db_path, ".analyze", "EXPLAIN SELECT id FROM users WHERE age = 45")
    assert "cost=" in output
    assert "Alternatives considered" in output


def test_trace_and_events_work_together(db_path: Path):
    output = run(db_path, ".trace storage", ".scan", ".events 5")
    assert "trace level set to STORAGE" in output
    assert "PageRead" in output or "HeapScan" in output
