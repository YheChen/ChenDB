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
Milestone 2 adds the SQL front end, Milestone 3 the volcano execution engine,
Milestone 4 a persistent catalog — so a database holds many tables and every
row-level method takes a table name — Milestone 5 disk-backed B+ tree indexes,
and Milestone 6 a cost-based planner that chooses between them using real
statistics, plus ``EXPLAIN``. See ``docs/roadmap.md``.
"""

from engine.catalog.catalog import Catalog, IndexInfo, TableInfo
from engine.database import Database
from engine.diagnostics import (
    EventCategory,
    RingBufferSink,
    TraceLevel,
    Tracer,
    TraceRecord,
)
from engine.errors import ChenDBError
from engine.index.bplustree import BPlusTree
from engine.serialization.record import Row
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.storage.constants import DEFAULT_PAGE_SIZE, PageType
from engine.storage.heap import RecordId

__version__ = "1.0.0"
"""Bumped once per milestone: 0.N.0 corresponds to Milestone N."""

MILESTONE = int(__version__.split(".")[0]) * 10 + int(__version__.split(".")[1])
"""Highest completed milestone, derived rather than declared.

It used to be written out in three places — here, the CLI banner and the
server's ``/health`` — and the CLI's copy sat one milestone behind for a whole
release because nothing failed when it drifted. So it is arithmetic on the
version instead, and ``test_architecture_boundaries`` pins the two together.

The roadmap's rule is ``0.N.0`` means Milestone N, which runs out at Milestone
10 — there is no ``0.10.0`` that sorts after ``0.9.0`` under semver. So the
tenth is ``1.0.0``, and the arithmetic carries: major * 10 + minor."""

MILESTONE_FEATURES: tuple[str, ...] = (
    "storage",
    "SQL",
    "execution",
    "catalog",
    "indexes",
    "planner",
    "buffer pool",
    "transactions",
    "write-ahead log",
    "MVCC",
)
"""What the engine can do, in the order the milestones added it.

Prose for banners, not a capability check — the API's ``/health`` has structured
flags for that. One entry per shipped milestone, so ``len()`` is a cheap
assertion that this list and :data:`MILESTONE` agree."""

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MILESTONE",
    "MILESTONE_FEATURES",
    "BPlusTree",
    "Catalog",
    "ChenDBError",
    "Column",
    "DataType",
    "Database",
    "EventCategory",
    "IndexInfo",
    "PageType",
    "RecordId",
    "RingBufferSink",
    "Row",
    "Schema",
    "TableInfo",
    "TraceLevel",
    "TraceRecord",
    "Tracer",
    "__version__",
]
