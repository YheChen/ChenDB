"""ChenDB. A relational database engine built from scratch in Python.

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
Milestone 4 a persistent catalog (so a database holds many tables and every
row-level method takes a table name) Milestone 5 disk-backed B+ tree indexes,
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

__version__ = "2.4.0"
"""Bumped once per milestone: 0.N.0 corresponds to Milestone N."""

MILESTONE = int(__version__.split(".")[0]) * 10 + int(__version__.split(".")[1])
"""Highest completed milestone, derived rather than declared.

It used to be written out in three places (here, the CLI banner and the
server's ``/health``) and the CLI's copy sat one milestone behind for a whole
release because nothing failed when it drifted. So it is arithmetic on the
version instead, and ``test_architecture_boundaries`` pins the two together.

The roadmap's rule is ``0.N.0`` means Milestone N, which runs out at Milestone
10. There is no ``0.10.0`` that sorts after ``0.9.0`` under semver. So the
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
    "UPDATE and DELETE",
    "joins and aggregation",
    "enforced primary keys",
    "outer joins",
    "outer-join simplification",
    "skew-aware statistics",
    "reordering across an outer join",
    "uncorrelated subqueries",
    "DISTINCT and IN",
)
"""What the engine can do, in the order the milestones added it.

Prose for banners, not a capability check. The API's ``/health`` has structured
flags for that.

It ran one entry per milestone for eleven milestones and then stopped. Some
milestones do not add a capability: Milestone 12 shipped continuous integration
and Milestone 14 a transport seam, and both are statements *about* the engine
rather than things it can do. "storage + SQL + execution + … + CI" is not a
sentence a banner should print.

Rather than loosen the check each time (it was ``==``, then ``>= MILESTONE - 1``,
and would have become ``- 2`` here) the exceptions are named in
:data:`MILESTONES_WITHOUT_ENGINE_FEATURES`. Naming them keeps the assertion
exact and makes skipping one a decision somebody had to write down."""

MILESTONES_WITHOUT_ENGINE_FEATURES: frozenset[int] = frozenset({12, 14, 15, 16, 21})
"""Milestones that shipped no new engine capability, and why.

* **12**: continuous integration, and a guard that runs every demo button.
* **14**: the transport seam, so the visualizer can carry the engine with it
  as WebAssembly instead of talking to a server.
* **15**: that build, shipped. The engine gained nothing: the same ``.py``
  files run, unmodified, in a browser tab. Being *deployable* somewhere new is
  not a thing a database can do.
* **16**: deployment and persistence for that build: Vercel, versioned cache
  paths, and the browser workspace backed by IndexedDB. *Where* the bytes are
  kept is not a capability either. The file format is byte-identical, and a
  page written in the browser has the same checksum it would on disk.

* **21**: a third table in the differential generator, and the two planner bugs
  it immediately found. Both were wrong answers, and fixing a wrong answer
  *restores* a capability the engine already claimed rather than adding one:
  "correct three-table joins" was on this list from Milestone 13, and was not
  true. The list records what each milestone added. This one removed two lies.

Milestone 19 is not on this list either, and it is the closest call so far. An
optimiser rewrite returns exactly the same rows by definition, so "the engine can
now simplify an outer join" is a statement about how a query runs and not about
what can be asked of it. The precedent that settles it is Milestone 7: a buffer
pool changes no answer either and is named above. A milestone whose whole content
is the engine getting faster at something belongs in the list.

Milestone 17 is *not* on this list, and that is worth a word. It shipped a
differential tester, which is a statement about the engine like Milestone 12's
CI, but it also found seven real bugs, and closing one of them gave the engine
something it could not do before: **a PRIMARY KEY is now enforced.** The index
machinery had existed since Milestone 5 and nothing connected it to the
constraint, which two milestone documents recorded as a known gap. That is a
capability, so it is named above rather than excused here.
"""

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MILESTONE",
    "MILESTONES_WITHOUT_ENGINE_FEATURES",
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
