"""The Milestone 1 headline: data written before a restart is there after it."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.database import Database
from engine.errors import ChenDBError, SchemaError
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

PAGE_SIZE = 256


def test_rows_survive_a_restart(db_path: Path, users_schema: Schema, sample_rows):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        db.insert_many(sample_rows)

    with Database.open(db_path) as db:
        assert db.rows() == [tuple(row) for row in sample_rows]


def test_the_schema_survives_a_restart(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)

    with Database.open(db_path) as db:
        assert db.table is not None
        assert db.table.name == "users"
        assert db.schema == users_schema
        assert db.schema.primary_key is not None
        assert db.schema.primary_key.name == "id"


def test_record_ids_remain_valid_across_a_restart(
    db_path: Path, users_schema: Schema, sample_rows
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        record_ids = db.insert_many(sample_rows)

    with Database.open(db_path) as db:
        for record_id, expected in zip(record_ids, sample_rows, strict=True):
            assert db.get(record_id) == tuple(expected)


def test_data_survives_many_restarts(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)

    # Each cycle appends rows and reopens, so later cycles read pages written
    # by earlier ones.
    for cycle in range(10):
        with Database.open(db_path) as db:
            db.insert((cycle, f"user-{cycle}", cycle * 2, cycle % 2 == 0, cycle / 4))
            assert db.count() == cycle + 1

    with Database.open(db_path) as db:
        rows = db.rows()
        assert len(rows) == 10
        assert [row[0] for row in rows] == list(range(10))


def test_deletes_are_persistent(db_path: Path, users_schema: Schema, sample_rows):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        record_ids = db.insert_many(sample_rows)
        db.delete(record_ids[1])

    with Database.open(db_path) as db:
        assert len(db.rows()) == len(sample_rows) - 1
        assert all(row[0] != sample_rows[1][0] for row in db.rows())


def test_a_multi_page_table_survives_a_restart(db_path: Path):
    schema = Schema.of(
        Column("id", DataType.INTEGER, nullable=False),
        Column("payload", DataType.TEXT, nullable=False),
    )
    row_count = 300

    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("wide", schema)
        db.insert_many([(i, f"value-{i:04d}") for i in range(row_count)])
        pages_written = db.page_count

    # A row here is 23 bytes (1 bitmap + 8 int + 4 length + 10 text) and costs
    # 27 with its slot, so a 232-byte usable page holds 8 rows: ~38 heap pages.
    assert pages_written > 30, "300 rows should span many 256-byte pages"

    with Database.open(db_path) as db:
        rows = db.rows()
        assert len(rows) == row_count
        assert rows[0] == (0, "value-0000")
        assert rows[-1] == (row_count - 1, f"value-{row_count - 1:04d}")
        assert db.page_count == pages_written


def test_sync_without_close_is_enough(db_path: Path, users_schema: Schema):
    # Proves durability comes from fsync, not from a tidy shutdown path.
    db = Database.open(db_path, page_size=PAGE_SIZE)
    db.create_table("users", users_schema)
    db.insert((1, "durable", None, True, 1.0))
    db.sync()

    with Database.open(db_path) as reopened:
        assert reopened.rows() == [(1, "durable", None, True, 1.0)]
    db.close()


def test_the_file_is_a_whole_number_of_pages(db_path: Path, users_schema: Schema):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        db.insert_many([(i, f"n{i}", i, True, 0.0) for i in range(50)])
        expected = db.page_count * PAGE_SIZE
    assert db_path.stat().st_size == expected


def test_only_one_table_per_database_in_milestone_1(
    db_path: Path, users_schema: Schema
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        with pytest.raises(SchemaError, match="Milestone 4"):
            db.create_table("orders", users_schema)


def test_operations_before_create_table_fail_clearly(db_path: Path):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        with pytest.raises(SchemaError, match="create_table"):
            db.insert((1,))
        with pytest.raises(SchemaError, match="create_table"):
            list(db.scan())
        assert db.table is None


def test_a_closed_database_refuses_work(db_path: Path, users_schema: Schema):
    db = Database.open(db_path, page_size=PAGE_SIZE)
    db.create_table("users", users_schema)
    db.close()
    db.close()  # idempotent
    assert db.closed
    with pytest.raises(ChenDBError, match="closed"):
        db.insert((1, "x", None, True, 0.0))


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
        db.insert_many(sample_rows)

    with Database.open(path) as db:
        assert db.page_size == page_size
        assert db.rows() == [tuple(row) for row in sample_rows]


def test_deleted_slots_can_be_reused_by_later_inserts(
    db_path: Path, users_schema: Schema
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        first = db.insert((1, "first", None, True, 0.0))
        db.insert((2, "second", None, True, 0.0))
        db.delete(first)

        # A heap is unordered: a new row may land in the freed slot, which is
        # exactly why SELECT without ORDER BY guarantees no ordering.
        db.insert((3, "third", None, True, 0.0))
        ids = sorted(row[0] for row in db.rows())
        assert ids == [2, 3]

    with Database.open(db_path) as db:
        assert sorted(row[0] for row in db.rows()) == [2, 3]
