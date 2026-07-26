"""Shared fixtures.

Small page sizes are used deliberately throughout the suite: a 256-byte page
holds three or four rows, so page-chaining, compaction and eviction paths are
exercised by a handful of inserts instead of hundreds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.database import Database
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.storage.pager import Pager

#: Big enough for a real record, small enough that a few rows fill a page.
TINY_PAGE_SIZE = 256


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.chendb"


@pytest.fixture
def users_schema() -> Schema:
    """A schema covering every type, both nullabilities, and a primary key."""
    return Schema.of(
        Column("id", DataType.INTEGER, nullable=False, primary_key=True),
        Column("name", DataType.TEXT, nullable=False),
        Column("age", DataType.INTEGER),
        Column("active", DataType.BOOLEAN),
        Column("score", DataType.FLOAT),
    )


@pytest.fixture
def sample_rows() -> list[tuple[object, ...]]:
    return [
        (1, "Ada Lovelace", 36, True, 99.5),
        (2, "Alan Turing", None, False, 12.25),
        (3, "Grace Hopper", 45, True, None),
        (4, "", 0, False, 0.0),
    ]


@pytest.fixture
def pager(db_path: Path) -> Pager:
    with Pager(db_path, page_size=TINY_PAGE_SIZE) as instance:
        yield instance


@pytest.fixture
def sink() -> RingBufferSink:
    return RingBufferSink(capacity=10_000)


@pytest.fixture
def tracer(sink: RingBufferSink) -> Tracer:
    return Tracer(sink, TraceLevel.VERBOSE)


@pytest.fixture
def db(db_path: Path, users_schema: Schema) -> Database:
    """An open database with the ``users`` table already created."""
    with Database.open(db_path, page_size=TINY_PAGE_SIZE) as instance:
        instance.create_table("users", users_schema)
        yield instance
