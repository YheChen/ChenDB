"""The headline property, now across many tables: data written before a restart
is there after it, and so is every schema.

Milestone 1 proved rows survive. Milestone 4 has to prove the *catalog* does —
that a reopened database can rediscover which tables exist, where their heaps
start, and what shape their rows are, with nothing held in memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.catalog.system import (
    COLUMNS_TABLE_NAME,
    FIRST_USER_TABLE_ID,
    TABLES_TABLE_NAME,
)
from engine.database import Database
from engine.errors import CatalogError, ChenDBError
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

PAGE_SIZE = 256

ORDERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("user_id", DataType.INTEGER, nullable=False),
    Column("total", DataType.FLOAT),
)


# -- rows ------------------------------------------------------------------


def test_rows_survive_a_restart(db_path: Path, users_schema: Schema, sample_rows):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        db.insert_many("users", sample_rows)

    with Database.open(db_path) as db:
        assert db.rows("users") == [tuple(row) for row in sample_rows]


def test_record_ids_remain_valid_across_a_restart(
    db_path: Path, users_schema: Schema, sample_rows
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        record_ids = db.insert_many("users", sample_rows)

    with Database.open(db_path) as db:
        for record_id, expected in zip(record_ids, sample_rows, strict=True):
            assert db.get("users", record_id) == tuple(expected)


def test_deletes_are_persistent(db_path: Path, users_schema: Schema, sample_rows):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        record_ids = db.insert_many("users", sample_rows)
        db.delete("users", record_ids[1])

    with Database.open(db_path) as db:
        assert len(db.rows("users")) == len(sample_rows) - 1


def test_data_survives_many_restarts(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)

    for cycle in range(10):
        with Database.open(db_path) as db:
            db.insert(
                "users",
                (cycle, f"user-{cycle}", cycle * 2, cycle % 2 == 0, cycle / 4),
            )
            assert db.count("users") == cycle + 1

    with Database.open(db_path) as db:
        assert [row[0] for row in db.rows("users")] == list(range(10))


def test_sync_without_close_is_enough(db_path: Path, users_schema: Schema):
    # Durability comes from fsync, not from a tidy shutdown path.
    db = Database.open(db_path, page_size=PAGE_SIZE)
    db.create_table("users", users_schema)
    db.insert("users", (1, "durable", None, True, 1.0))
    db.sync()

    with Database.open(db_path) as reopened:
        assert reopened.rows("users") == [(1, "durable", None, True, 1.0)]
    db.close()


# -- the catalog -----------------------------------------------------------


def test_a_schema_is_rebuilt_from_the_catalog_after_a_restart(
    db_path: Path, users_schema: Schema
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)

    with Database.open(db_path) as db:
        # Nothing is held in memory: this comes from chendb_columns.
        assert db.schema_of("users") == users_schema
        primary = db.schema_of("users").primary_key
        assert primary is not None and primary.name == "id"


def test_column_order_survives_because_position_is_stored(db_path: Path):
    # A heap scan returns physical order, which is not column order. The
    # `position` column is what makes the rebuild deterministic.
    wide = Schema(
        tuple(
            Column(f"c{index}", DataType.INTEGER, nullable=index > 0)
            for index in range(12)
        )
    )
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("wide", wide)

    with Database.open(db_path) as db:
        assert db.schema_of("wide").column_names == wide.column_names


def test_every_column_attribute_round_trips(db_path: Path):
    schema = Schema.of(
        Column("pk", DataType.INTEGER, nullable=False, primary_key=True),
        Column("required", DataType.TEXT, nullable=False),
        Column("optional", DataType.FLOAT, nullable=True),
        Column("flag", DataType.BOOLEAN, nullable=True),
    )
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("t", schema)

    with Database.open(db_path) as db:
        restored = db.schema_of("t")
        assert restored == schema
        assert restored[0].primary_key is True
        assert restored[1].nullable is False
        assert restored[2].data_type is DataType.FLOAT


def test_many_tables_coexist_and_all_survive(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        db.create_table("orders", ORDERS)
        db.insert_many("users", [(1, "ada", 36, True, 1.0), (2, "alan", None, False, 2.0)])
        db.insert_many("orders", [(1, 1, 9.99), (2, 1, 24.5), (3, 2, 5.0)])

    with Database.open(db_path) as db:
        assert db.table_names() == ["orders", "users"]
        assert len(db.rows("users")) == 2
        assert len(db.rows("orders")) == 3
        # Each table's rows decode with its own schema, which is the whole point.
        assert db.rows("orders")[0] == (1, 1, 9.99)


def test_table_ids_are_stable_and_start_above_the_reserved_range(
    db_path: Path, users_schema: Schema
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        first = db.create_table("users", users_schema)
        second = db.create_table("orders", ORDERS)
        assert first.table_id == FIRST_USER_TABLE_ID
        assert second.table_id == FIRST_USER_TABLE_ID + 1

    with Database.open(db_path) as db:
        assert db.require_table("users").table_id == first.table_id
        assert db.require_table("orders").table_id == second.table_id
        # A third table must not reuse an id after a restart.
        third = db.create_table("items", ORDERS)
        assert third.table_id == FIRST_USER_TABLE_ID + 2


def test_the_system_tables_are_readable_like_any_other(
    db_path: Path, users_schema: Schema
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)

        names = [info.name for info in db.tables(include_system=True)]
        assert TABLES_TABLE_NAME in names
        assert COLUMNS_TABLE_NAME in names
        # ...but they are hidden by default, because a schema browser should
        # show the user's tables first.
        assert TABLES_TABLE_NAME not in db.table_names()

        catalog_rows = db.rows(TABLES_TABLE_NAME)
        assert any(row[1] == "users" for row in catalog_rows)
        column_rows = db.rows(COLUMNS_TABLE_NAME)
        assert len(column_rows) == len(users_schema)


def test_a_table_whose_heap_grew_reopens_at_the_right_page(db_path: Path):
    """The catalog row must follow the heap when it extends.

    If ``last_page`` were stale, a reopened database would append into the middle
    of the chain — silently, and only for tables big enough to have spilled.
    """
    schema = Schema.of(
        Column("id", DataType.INTEGER, nullable=False),
        Column("payload", DataType.TEXT, nullable=False),
    )
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("wide", schema)
        db.insert_many("wide", [(i, f"value-{i:04d}") for i in range(200)])
        info = db.require_table("wide")
        assert info.last_page != info.first_page, "200 rows must span pages"
        pages_before = db.page_count

    with Database.open(db_path) as db:
        reopened = db.require_table("wide")
        assert reopened.last_page == info.last_page
        db.insert("wide", (999, "appended-after-restart"))
        # An append must extend the tail, not re-walk from the start.
        assert db.page_count in (pages_before, pages_before + 1)
        assert db.count("wide") == 201
        assert db.rows("wide")[-1] == (999, "appended-after-restart")


def test_creating_a_duplicate_table_is_refused(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        with pytest.raises(CatalogError, match="already exists"):
            db.create_table("users", users_schema)
        with pytest.raises(CatalogError, match="already exists"):
            db.create_table("USERS", users_schema)


def test_a_reserved_name_is_refused(db_path: Path, users_schema: Schema):
    with (
        Database.open(db_path, page_size=PAGE_SIZE) as db,
        pytest.raises(CatalogError, match="reserved"),
    ):
        db.create_table("chendb_sneaky", users_schema)


def test_an_unknown_table_lists_what_does_exist(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        assert db.table("nope") is None
        with pytest.raises(CatalogError, match="users"):
            db.require_table("nope")


def test_lookups_are_case_insensitive(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("Users", users_schema)
        assert db.require_table("users").name == "Users", "declared case is kept"
        assert db.require_table("USERS").name == "Users"


def test_the_catalog_cache_avoids_rescanning(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        db.catalog.invalidate()

        db.require_table("users")  # a miss: two full catalog scans
        scans_after_miss = db.catalog.stats.scans
        for _ in range(20):
            db.require_table("users")
        assert db.catalog.stats.scans == scans_after_miss
        assert db.catalog.stats.hit_rate > 0.9


# -- file-level invariants -------------------------------------------------


def test_the_file_is_a_whole_number_of_pages(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        db.insert_many("users", [(i, f"n{i}", i, True, 0.0) for i in range(50)])
        expected = db.page_count * PAGE_SIZE
    assert db_path.stat().st_size == expected


def test_page_size_is_fixed_at_creation(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=512) as db:
        db.create_table("users", users_schema)
    with Database.open(db_path) as db:
        assert db.page_size == 512


@pytest.mark.parametrize("page_size", [256, 512, 1024, 4096, 8192])
def test_every_supported_page_size_roundtrips(
    tmp_path: Path, users_schema: Schema, sample_rows, page_size: int
):
    path = tmp_path / f"db-{page_size}.chendb"
    with Database.open(path, page_size=page_size) as db:
        db.create_table("users", users_schema)
        db.insert_many("users", sample_rows)

    with Database.open(path) as db:
        assert db.page_size == page_size
        assert db.rows("users") == [tuple(row) for row in sample_rows]
        assert db.schema_of("users") == users_schema


def test_deleted_slots_can_be_reused_by_later_inserts(
    db_path: Path, users_schema: Schema
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        first = db.insert("users", (1, "first", None, True, 0.0))
        db.insert("users", (2, "second", None, True, 0.0))
        db.delete("users", first)
        db.insert("users", (3, "third", None, True, 0.0))
        assert sorted(row[0] for row in db.rows("users")) == [2, 3]

    with Database.open(db_path) as db:
        assert sorted(row[0] for row in db.rows("users")) == [2, 3]


def test_a_new_database_has_a_catalog_but_no_user_tables(db_path: Path):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        assert db.table_names() == []
        assert db.catalog.initialised
        # Two heaps for the two system tables, plus the meta page.
        assert db.page_count == 3


def test_operations_on_a_missing_table_fail_clearly(db_path: Path):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        with pytest.raises(CatalogError, match="no table named"):
            db.insert("users", (1,))
        with pytest.raises(CatalogError, match="no table named"):
            list(db.scan("users"))


def test_a_closed_database_refuses_work(db_path: Path, users_schema: Schema):
    db = Database.open(db_path, page_size=PAGE_SIZE)
    db.create_table("users", users_schema)
    db.close()
    db.close()  # idempotent
    assert db.closed
    with pytest.raises(ChenDBError, match="closed"):
        db.insert("users", (1, "x", None, True, 0.0))
