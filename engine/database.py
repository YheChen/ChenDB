"""``Database`` — the public face of the engine.

    from engine import Column, DataType, Database, Schema

    with Database.open("shop.chendb") as db:
        db.create_table("users", Schema.of(
            Column("id", DataType.INTEGER, nullable=False, primary_key=True),
            Column("email", DataType.TEXT, nullable=False),
        ))
        db.insert("users", (1, "ada@example.com"))

        for record_id, row in db.scan("users"):
            print(record_id, row)

It composes the layers underneath rather than reimplementing them::

    Database          rows of Python values, many tables
        │
        ├── Catalog           what tables exist, and where          (M4)
        │       │
        │       └── HeapFile      chendb_tables, chendb_columns
        │
        ├── HeapFile          one per user table, addressed by RecordId
        │       │
        │       └── Pager     pages as bytes, addressed by page id
        │               │
        │               └── the file
        │
        └── serialization     Schema + codecs: values ⇄ bytes

Milestone 4 scope
-----------------
Every method that touches rows now takes a table name.  That is a breaking change
from Milestones 1-3, which had exactly one table per file and therefore did not
need one; the single JSON schema page and the ``heap_first_page`` meta fields are
both gone.

There are still no transactions.  Every :meth:`insert` writes through to the OS
immediately, and :meth:`sync` is what makes it durable.  Creating a table writes
several rows across two system tables and is *not* atomic: a crash part-way could
leave a table with columns but no ``chendb_tables`` row, or an orphaned heap.
Fixing that is what the write-ahead log in Milestone 9 is for.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

from engine.catalog.catalog import Catalog, TableInfo
from engine.diagnostics.events import DatabaseClosedEvent, DatabaseOpenedEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import CatalogError, ChenDBError
from engine.serialization.record import (
    RecordLayout,
    Row,
    decode_record,
    describe_record,
    encode_record,
)
from engine.serialization.schema import Schema
from engine.storage.constants import DEFAULT_PAGE_SIZE
from engine.storage.heap import HeapFile, RecordId
from engine.storage.inspect import (
    PageDetail,
    PageSummary,
    inspect_page,
    iter_page_summaries,
)
from engine.storage.page import Page
from engine.storage.pager import Pager, PagerStats

__all__ = ["DATABASE_SUFFIX", "Database"]

#: Conventional extension. Nothing enforces it; it just makes files obvious.
DATABASE_SUFFIX = ".chendb"


class Database:
    """A single ChenDB database file, holding any number of tables."""

    __slots__ = ("_catalog", "_closed", "_database_id", "_pager", "_tracer")

    def __init__(
        self,
        pager: Pager,
        *,
        tracer: Tracer | None = None,
        database_id: str | None = None,
        create: bool = False,
    ) -> None:
        self._pager = pager
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._database_id = database_id or pager.path.stem
        self._closed = False
        self._catalog = Catalog(pager, tracer=self._tracer)
        if create and not self._catalog.initialised:
            self._catalog.bootstrap()

    # -- opening -----------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        create: bool = True,
        page_size: int = DEFAULT_PAGE_SIZE,
        tracer: Tracer | None = None,
        verify_checksums: bool = True,
        database_id: str | None = None,
    ) -> Database:
        """Open ``path``, creating and initialising it if it does not exist."""
        resolved = Path(path)
        existed = resolved.exists() and resolved.stat().st_size > 0
        pager = Pager(
            resolved,
            page_size=page_size,
            create=create,
            verify_checksums=verify_checksums,
            tracer=tracer,
        )
        db = cls(pager, tracer=tracer, database_id=database_id, create=True)
        if db._tracer.summary:
            db._tracer.emit(
                DatabaseOpenedEvent(
                    database_id=db._database_id,
                    page_size=pager.page_size,
                    page_count=pager.page_count,
                    created=not existed,
                )
            )
        return db

    # -- catalog -----------------------------------------------------------

    @property
    def catalog(self) -> Catalog:
        """The system catalog. Read it directly for statistics or listings."""
        return self._catalog

    def create_table(self, name: str, schema: Schema) -> TableInfo:
        """Define a new table. Raises if the name is taken or reserved."""
        self._ensure_open()
        return self._catalog.create_table(name, schema)

    def table(self, name: str) -> TableInfo | None:
        """Look up a table, or ``None`` if it does not exist."""
        self._ensure_open()
        return self._catalog.get_table(name)

    def require_table(self, name: str) -> TableInfo:
        """Look up a table, raising :class:`CatalogError` if it is absent."""
        self._ensure_open()
        return self._catalog.require_table(name)

    def tables(self, *, include_system: bool = False) -> list[TableInfo]:
        """Every table in the database, user tables first."""
        self._ensure_open()
        return self._catalog.list_tables(include_system=include_system)

    def table_names(self) -> list[str]:
        return [info.name for info in self.tables()]

    def schema_of(self, name: str) -> Schema:
        return self.require_table(name).schema

    def heap_for(self, name: str) -> HeapFile:
        """The heap holding ``name``'s rows. For the executor's scans."""
        self._ensure_open()
        return self._catalog.heap_for(name)

    # -- rows --------------------------------------------------------------

    def insert(self, table: str, values: Sequence[Any]) -> RecordId:
        """Encode ``values`` and append them to ``table``."""
        info = self.require_table(table)
        return self._catalog.heap_for(info.name).insert(
            encode_record(info.schema, values)
        )

    def insert_many(
        self, table: str, rows: Sequence[Sequence[Any]]
    ) -> list[RecordId]:
        """Insert several rows. Still one page write per row in Milestone 4.

        A real bulk loader fills a page in memory and writes it once; that becomes
        possible when the buffer pool lands in Milestone 7.
        """
        info = self.require_table(table)
        heap = self._catalog.heap_for(info.name)
        return [heap.insert(encode_record(info.schema, row)) for row in rows]

    def get(self, table: str, record_id: RecordId) -> Row:
        """Fetch and decode one row by its physical address."""
        info = self.require_table(table)
        return decode_record(
            info.schema, self._catalog.heap_for(info.name).get(record_id)
        )

    def describe(self, table: str, record_id: RecordId) -> RecordLayout:
        """Fetch one row along with each column's byte range."""
        info = self.require_table(table)
        return describe_record(
            info.schema, self._catalog.heap_for(info.name).get(record_id)
        )

    def scan(self, table: str) -> Iterator[tuple[RecordId, Row]]:
        """Yield every live row of ``table``, lazily, in physical order.

        Physical order is *not* insertion order after deletes: a tombstoned slot
        can be reused by a later row. Heaps are unordered by definition, which is
        why ``SELECT`` without ``ORDER BY`` guarantees nothing.
        """
        info = self.require_table(table)
        for record_id, payload in self._catalog.heap_for(info.name).scan():
            yield record_id, decode_record(info.schema, payload)

    def rows(self, table: str) -> list[Row]:
        """Materialise every row. Convenience for tests and small tables."""
        return [row for _, row in self.scan(table)]

    def delete(self, table: str, record_id: RecordId) -> bool:
        """Tombstone one row. Returns ``False`` if it was already gone."""
        info = self.require_table(table)
        return self._catalog.heap_for(info.name).delete(record_id)

    def count(self, table: str) -> int:
        """Live row count. O(pages) — there is no cached count."""
        info = self.require_table(table)
        return self._catalog.heap_for(info.name).count()

    # -- introspection -----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._pager.path

    @property
    def database_id(self) -> str:
        return self._database_id

    @property
    def page_size(self) -> int:
        return self._pager.page_size

    @property
    def page_count(self) -> int:
        return self._pager.page_count

    @property
    def pager(self) -> Pager:
        """Escape hatch for tools and tests that need page-level access."""
        return self._pager

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def stats(self) -> PagerStats:
        return self._pager.stats

    def heap_page_ids(self, table: str) -> frozenset[int]:
        """Page ids belonging to one table's heap."""
        info = self.table(table)
        if info is None:
            return frozenset()
        return frozenset(self._catalog.heap_for(info.name).page_ids())

    def page_owners(self) -> dict[int, str]:
        """Which table each page belongs to, for the disk map.

        Walks every table's chain, so it costs O(pages) reads. Only the inspector
        calls it.
        """
        self._ensure_open()
        owners: dict[int, str] = {}
        for info in self._catalog.list_tables(include_system=True):
            try:
                heap = self._catalog.heap_for(info.name)
            except CatalogError:  # pragma: no cover - listed means present
                continue
            for page_id in heap.page_ids():
                owners[page_id] = info.name
        return owners

    def page_summaries(self) -> list[PageSummary]:
        """Summarize every page in the file, in page order."""
        self._ensure_open()
        return iter_page_summaries(
            self._pager, range(self._pager.page_count), owners=self.page_owners()
        )

    def page_detail(self, page_id: int) -> PageDetail:
        """Fully inspect one page, decoding its records where possible."""
        self._ensure_open()
        owners = self.page_owners()
        table_name = owners.get(page_id)
        schema = None
        if table_name is not None:
            info = self._catalog.get_table(table_name)
            schema = info.schema if info else None
        return inspect_page(self._pager, page_id, schema=schema, owners=owners)

    def read_page(self, page_id: int) -> Page:
        """Read a decoded slotted page. Page 0 is the meta page and is rejected."""
        return self._pager.read_page(page_id)

    # -- lifecycle ---------------------------------------------------------

    def sync(self) -> None:
        """Force everything written so far to durable storage."""
        self._pager.sync()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ChenDBError(f"database {self._database_id!r} is closed")

    def close(self) -> None:
        """Sync and release the file handle. Idempotent."""
        if self._closed:
            return
        if self._tracer.summary:
            self._tracer.emit(
                DatabaseClosedEvent(
                    database_id=self._database_id,
                    page_count=self._pager.page_count,
                    pages_written=self._pager.stats.page_writes,
                )
            )
        self._pager.close()
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        tables = "?" if self._closed else len(self._catalog.list_tables())
        return (
            f"<Database {self._database_id!r} {state} "
            f"tables={tables} pages={self._pager.page_count}>"
        )
