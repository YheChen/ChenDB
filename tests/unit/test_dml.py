"""``UPDATE ... SET`` and ``DELETE ... WHERE``.

Everything before Milestone 11 could write a row and take it away again. What
was missing is the case in between: a row that *changes*, which is the one MVCC
was designed around and the only one that produces a version chain more than one
link long.

The tests are grouped by what can go wrong rather than by statement, because the
two statements share a row source and therefore share most of their failure
modes: locating the wrong rows, changing them more than once, forgetting an
index, or reporting a number that is not what happened.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.errors import BindingError, ExecutionError
from engine.executor.engine import execute_script
from engine.planner.physical import PhysicalIndexScan, PhysicalSeqScan, walk_physical
from engine.serialization.record import read_tuple_header

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
    Column("pay", DataType.INTEGER),
)
ROWS = [
    (1, "ada", 40_000),
    (2, "alan", 45_000),
    (3, "grace", 60_000),
    (4, "edsger", 75_000),
]


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "dml.chendb", page_size=512) as handle:
        handle.create_table("staff", SCHEMA)
        handle.insert_many("staff", ROWS)
        yield handle


def run(db: Database, sql: str):
    """Run a script and return the last statement's result."""
    return execute_script(sql, db)[-1]


def pay(db: Database) -> dict[str, int]:
    return {row[1]: row[2] for row in db.rows("staff")}


# -- the basics ------------------------------------------------------------


def test_delete_removes_only_the_matching_rows(db: Database):
    result = run(db, "DELETE FROM staff WHERE pay > 50000")
    assert result.stats.rows_affected == 2
    assert sorted(pay(db)) == ["ada", "alan"]


def test_delete_without_a_predicate_removes_everything(db: Database):
    assert run(db, "DELETE FROM staff").stats.rows_affected == 4
    assert db.count("staff") == 0


def test_update_changes_the_named_columns_and_nothing_else(db: Database):
    run(db, "UPDATE staff SET pay = 41000 WHERE name = 'ada'")
    assert pay(db) == {"ada": 41_000, "alan": 45_000, "grace": 60_000, "edsger": 75_000}


def test_an_update_can_read_the_row_it_replaces(db: Database):
    run(db, "UPDATE staff SET pay = pay + 1000")
    assert sorted(pay(db).values()) == [41_000, 46_000, 61_000, 76_000]


def test_assignments_all_see_the_old_row_so_a_swap_swaps(db: Database):
    # The one thing about SET that surprises people from a procedural language:
    # this is not `id = pay; pay = id`.
    run(db, "UPDATE staff SET id = pay, pay = id WHERE name = 'ada'")
    row = next(r for r in db.rows("staff") if r[1] == "ada")
    assert (row[0], row[2]) == (40_000, 1)


def test_neither_statement_returns_rows(db: Database):
    assert not run(db, "UPDATE staff SET pay = 1").returns_rows
    assert not run(db, "DELETE FROM staff").returns_rows


def test_a_predicate_matching_nothing_changes_nothing(db: Database):
    result = run(db, "DELETE FROM staff WHERE pay < 0")
    assert result.stats.rows_affected == 0
    assert result.stats.rows_scanned == 4
    assert db.count("staff") == 4


# -- the Halloween problem -------------------------------------------------


def test_an_update_that_keeps_matching_its_own_predicate_still_runs_once(
    db: Database,
):
    """The Halloween problem, in the form that would loop.

    Every raise keeps the row under 50,000, so a scan that saw its own new
    versions would raise ada and alan again and again until they escaped the
    predicate, 40,000 would end at 50,000 rather than 42,000. Materialising the
    row set before writing is what stops it.
    """
    result = run(db, "UPDATE staff SET pay = pay + 2000 WHERE pay < 50000")
    assert result.stats.rows_affected == 2
    assert pay(db)["ada"] == 42_000
    assert pay(db)["alan"] == 47_000


def test_an_update_of_an_indexed_column_does_not_revisit_its_own_rows(db: Database):
    # Same hazard through the other access path: an index scan descends to the
    # new entry too, and the new version's key is still inside the range.
    db.create_index("pay_idx", "staff", "pay")
    run(db, "UPDATE staff SET pay = pay + 2000 WHERE pay < 50000")
    assert pay(db)["ada"] == 42_000


# -- version chains --------------------------------------------------------


def test_an_update_leaves_the_old_version_in_place(db: Database):
    before = db.version_count("staff")
    run(db, "UPDATE staff SET pay = 1 WHERE name = 'ada'")
    assert db.version_count("staff") == before + 1
    assert db.count("staff") == 4


def test_the_old_version_carries_the_updating_transaction_as_its_xmax(db: Database):
    run(db, "UPDATE staff SET pay = 1 WHERE name = 'ada'")

    headers = [read_tuple_header(payload) for _, payload in db.heap_for("staff").scan()]
    dead = [header for header in headers if header.deleted]
    live = [header for header in headers if not header.deleted]

    assert len(dead) == 1, "exactly one version was superseded"
    # The same transaction ended the old version and began the new one, which is
    # what makes the pair an update rather than an unrelated delete and insert.
    assert dead[0].xmax == max(header.xmin for header in live)


def test_vacuum_reclaims_the_superseded_version(db: Database):
    run(db, "UPDATE staff SET pay = 1 WHERE name = 'ada'")
    assert db.vacuum("staff") == 1
    assert db.version_count("staff") == 4
    assert db.count("staff") == 4


# -- indexes ---------------------------------------------------------------


def test_an_update_moves_the_index_entry_to_the_new_version(db: Database):
    db.create_index("pay_idx", "staff", "pay")
    run(db, "UPDATE staff SET pay = 99000 WHERE name = 'ada'")

    assert db.lookup("pay_idx", 40_000) == []
    assert [row[1] for row in db.lookup("pay_idx", 99_000)] == ["ada"]


def test_an_update_rewrites_indexes_on_columns_it_did_not_touch(db: Database):
    """Because the row's *address* changed, not its value.

    This is the cost PostgreSQL's heap-only tuples exist to avoid, and the
    reason an MVCC update is more expensive than it looks.
    """
    db.create_index("name_idx", "staff", "name")
    run(db, "UPDATE staff SET pay = 99000 WHERE name = 'ada'")

    found = db.lookup("name_idx", "ada")
    assert [row[2] for row in found] == [99_000], "the entry points at the new version"


def test_a_delete_removes_the_index_entry(db: Database):
    db.create_index("pay_idx", "staff", "pay")
    run(db, "DELETE FROM staff WHERE name = 'ada'")
    assert db.lookup("pay_idx", 40_000) == []


# -- planning --------------------------------------------------------------


def test_a_delete_by_indexed_equality_uses_the_index(db: Database):
    # The whole reason DELETE goes through the planner: without it, removing one
    # row out of a million would read all million.
    db.create_index("pay_idx", "staff", "pay")
    db.analyze("staff")
    result = run(db, "DELETE FROM staff WHERE pay = 40000")
    assert any(
        isinstance(node, PhysicalIndexScan) for node in walk_physical(result.planned.root)
    )


def test_an_unindexed_update_falls_back_to_a_sequential_scan(db: Database):
    result = run(db, "UPDATE staff SET pay = 1 WHERE name = 'ada'")
    assert any(
        isinstance(node, PhysicalSeqScan) for node in walk_physical(result.planned.root)
    )


def test_explain_shows_the_row_source_and_says_what_follows_it(db: Database):
    plan = "\n".join(row[0] for row in run(db, "EXPLAIN DELETE FROM staff").rows)
    assert plan.startswith("Delete on staff")
    assert "set xmax" in plan


def test_explain_analyze_actually_performs_the_change(db: Database):
    # PostgreSQL's behaviour, and the reason its docs tell you to wrap the
    # statement in a transaction you mean to roll back.
    run(db, "EXPLAIN ANALYZE DELETE FROM staff WHERE name = 'ada'")
    assert "ada" not in pay(db)


# -- binding ---------------------------------------------------------------


def test_assigning_to_a_column_that_does_not_exist_is_rejected(db: Database):
    with pytest.raises(BindingError, match="no column named 'nope'"):
        run(db, "UPDATE staff SET nope = 1")


def test_assigning_the_same_column_twice_is_rejected(db: Database):
    with pytest.raises(BindingError, match="assigned twice"):
        run(db, "UPDATE staff SET pay = 1, pay = 2")


def test_assigning_the_wrong_type_is_caught_before_anything_is_written(db: Database):
    with pytest.raises(BindingError, match="cannot assign TEXT to column 'pay'"):
        run(db, "UPDATE staff SET pay = 'lots'")
    assert pay(db)["ada"] == 40_000


def test_assigning_null_to_a_not_null_column_is_rejected(db: Database):
    with pytest.raises(BindingError, match="NOT NULL"):
        run(db, "UPDATE staff SET name = NULL")


def test_assigning_null_to_a_nullable_column_is_allowed(db: Database):
    run(db, "UPDATE staff SET pay = NULL WHERE name = 'ada'")
    assert pay(db)["ada"] is None


def test_an_integer_widens_into_a_float_column(tmp_path: Path):
    with Database.open(tmp_path / "widen.chendb") as db:
        db.create_table("m", Schema.of(Column("score", DataType.FLOAT)))
        db.insert("m", (1.5,))
        run(db, "UPDATE m SET score = 2")
        assert db.rows("m") == [(2.0,)]


def test_deleting_from_a_table_that_does_not_exist_names_the_ones_that_do(
    db: Database,
):
    with pytest.raises(BindingError, match=r"no table named 'nope'.*has staff"):
        run(db, "DELETE FROM nope")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM chendb_tables",
        "UPDATE chendb_columns SET column_name = 'x'",
        "INSERT INTO chendb_tables VALUES (9, 'x', 1, 1, 0)",
    ],
)
def test_the_catalog_cannot_be_written_to_through_sql(db: Database, sql: str):
    # It is readable (that is how the schema view works) but a DELETE here
    # would drop a table's definition out from under the heap holding its rows.
    with pytest.raises(BindingError, match="system table"):
        run(db, sql)


# -- transactions ----------------------------------------------------------


def test_a_rolled_back_delete_puts_every_row_back(db: Database):
    execute_script("BEGIN; DELETE FROM staff;", db)
    assert db.count("staff") == 0
    execute_script("ROLLBACK", db)
    assert db.count("staff") == 4


def test_a_rolled_back_update_leaves_no_version_behind(db: Database):
    versions = db.version_count("staff")
    execute_script("BEGIN; UPDATE staff SET pay = 1;", db)
    execute_script("ROLLBACK", db)
    assert pay(db)["ada"] == 40_000
    # Rollback restores pages, so the new versions are physically gone rather
    # than left for the vacuum: which is why ChenDB needs no commit log.
    assert db.version_count("staff") == versions


def test_a_failed_statement_dooms_the_whole_script(db: Database):
    with pytest.raises(BindingError):
        execute_script("DELETE FROM staff WHERE pay > 50000; DELETE FROM nope;", db)
    assert db.count("staff") == 4


def test_one_statement_is_one_transaction_for_every_row_it_changes(db: Database):
    run(db, "DELETE FROM staff")
    heap = db.heap_for("staff")
    stamps = {read_tuple_header(payload).xmax for _, payload in heap.scan()}
    assert len(stamps) == 1, "all four rows carry the same deleting transaction"


# -- limits ----------------------------------------------------------------


def test_a_statement_matching_more_rows_than_the_ceiling_refuses_to_run_at_all(
    tmp_path: Path,
):
    # Changing the first N and reporting success is the one outcome worse than
    # failing, so the ceiling is a refusal rather than a truncation.
    with Database.open(tmp_path / "many.chendb") as db:
        db.create_table("m", Schema.of(Column("n", DataType.INTEGER)))
        db.insert_many("m", [(n,) for n in range(20)])
        with pytest.raises(ExecutionError, match="will not change part"):
            execute_script("DELETE FROM m", db, max_rows=5)
        assert db.count("m") == 20
