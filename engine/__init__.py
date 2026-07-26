"""ChenDB — a relational database engine built from scratch in Python.

The engine depends on nothing but the standard library and knows nothing about
HTTP, JSON or the visualizer.  It can be embedded::

    from engine import Column, DataType, Database, Schema

    with Database.open("shop.chendb") as db:
        db.create_table("users", Schema.of(
            Column("id", DataType.INTEGER, nullable=False, primary_key=True),
            Column("name", DataType.TEXT),
        ))
        db.insert((1, "Ada"))

run interactively::

    python -m engine shop.chendb

or observed, by attaching a diagnostics sink::

    from engine.diagnostics import RingBufferSink, Tracer, TraceLevel

    sink = RingBufferSink()
    db = Database.open("shop.chendb", tracer=Tracer(sink, TraceLevel.STORAGE))

Milestone 1 implements the storage engine: fixed-size pages, a slotted page
layout, binary record encoding, a heap file and a persistent metadata page.
SQL, planning, execution, indexes, transactions, WAL and MVCC follow in
Milestones 2-10; see ``docs/roadmap.md``.
"""

from engine.database import Database
from engine.diagnostics import (
    EventCategory,
    RingBufferSink,
    TraceLevel,
    Tracer,
    TraceRecord,
)
from engine.errors import ChenDBError
from engine.serialization.record import Row
from engine.serialization.schema import Column, Schema, TableDescriptor
from engine.serialization.types import DataType
from engine.storage.constants import DEFAULT_PAGE_SIZE, PageType
from engine.storage.heap import RecordId

__version__ = "0.1.0"
"""Bumped once per milestone: 0.N.0 corresponds to Milestone N."""

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "ChenDBError",
    "Column",
    "DataType",
    "Database",
    "EventCategory",
    "PageType",
    "RecordId",
    "RingBufferSink",
    "Row",
    "Schema",
    "TableDescriptor",
    "TraceLevel",
    "TraceRecord",
    "Tracer",
    "__version__",
]
