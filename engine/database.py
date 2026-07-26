"""``Database`` — the public face of the engine.

This is the object an embedding application holds::

    from engine import Database, Schema, Column, DataType

    with Database.open("shop.chendb") as db:
        db.create_table("users", Schema.of(
            Column("id", DataType.INTEGER, nullable=False, primary_key=True),
            Column("email", DataType.TEXT, nullable=False),
        ))
        db.insert((1, "ada@example.com"))
        for record_id, row in db.scan():
            print(record_id, row)

It composes the layers underneath rather than reimplementing them::

    Database          rows of Python values, one table
        │
        ├── HeapFile          records as bytes, addressed by RecordId
        │       │
        │       └── Pager     pages as bytes, addressed by page id
        │               │
        │               └── the file
        │
        └── serialization     Schema + codecs: values ⇄ bytes

Milestone 1 scope
-----------------
One table per database file, created through :meth:`Database.create_table`
rather than SQL, because there is no parser yet.  The schema is persisted as
JSON on a dedicated page so a reopened database can still decode its rows;
Milestone 4 replaces that with a real catalog and lifts the one-table limit.

There are no transactions.  Every :meth:`insert` writes through to the OS
immediately, and :meth:`sync` is what makes it durable.  A crash between the
two loses recent rows, and a crash *during* a page write can tear it — the
checksum will catch that on the next read, but nothing can repair it until the
write-ahead log arrives in Milestone 9.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

from engine.diagnostics.events import DatabaseClosedEvent, DatabaseOpenedEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import ChenDBError, CorruptDatabaseError, SchemaError
from engine.serialization.record import (
    RecordLayout,
    Row,
    decode_record,
    describe_record,
    encode_record,
)
from engine.serialization.schema import Schema, TableDescriptor
from engine.storage.constants import DEFAULT_PAGE_SIZE, INVALID_PAGE_ID, PageType
from engine.storage.heap import HeapFile, RecordId
from engine.storage.inspect import (
    PageDetail,
    PageSummary,
    inspect_page,
    iter_page_summaries,
)
from engine.storage.page import Page
from engine.storage.pager import Pager, PagerStats

__all__ = ["Database"]

#: Conventional extension. Nothing enforces it; it just makes files obvious.
DATABASE_SUFFIX = ".chendb"

#: The schema descriptor always occupies slot 0 of its page.
_SCHEMA_SLOT_ID = 0


class Database:
    """A single ChenDB database file."""

    __slots__ = ("_closed", "_database_id", "_descriptor", "_heap", "_pager", "_tracer")

    def __init__(
        self,
        pager: Pager,
        *,
        tracer: Tracer | None = None,
        database_id: str | None = None,
    ) -> None:
        self._pager = pager
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._database_id = database_id or pager.path.stem
        self._descriptor: TableDescriptor | None = None
        self._heap: HeapFile | None = None
        self._closed = False
        self._load_existing_table()

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
        """Open ``path``, creating it if it does not exist."""
        resolved = Path(path)
        existed = resolved.exists() and resolved.stat().st_size > 0
        pager = Pager(
            resolved,
            page_size=page_size,
            create=create,
            verify_checksums=verify_checksums,
            tracer=tracer,
        )
        db = cls(pager, tracer=tracer, database_id=database_id)
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

    def _load_existing_table(self) -> None:
        """Restore the table descriptor and heap handle from the file."""
        meta = self._pager.meta
        if meta.schema_page_id == INVALID_PAGE_ID:
            return

        self._descriptor = TableDescriptor.from_json(
            self._read_schema_chain(meta.schema_page_id)
        )

        if meta.heap_first_page == INVALID_PAGE_ID:
            raise CorruptDatabaseError(
                f"table {self._descriptor.name!r} has a schema but no heap pages"
            )
        self._heap = HeapFile(
            self._pager,
            meta.heap_first_page,
            meta.heap_last_page,
            on_pages_changed=self._on_heap_pages_changed,
            tracer=self._tracer,
        )

    def _read_schema_chain(self, first_page_id: int) -> bytes:
        """Reassemble the table descriptor from its chain of SCHEMA pages."""
        chunks: list[bytes] = []
        page_id = first_page_id
        seen: set[int] = set()
        while page_id != INVALID_PAGE_ID:
            if page_id in seen:
                raise CorruptDatabaseError(f"cycle in schema page chain at {page_id}")
            seen.add(page_id)
            page = self._pager.read_page(page_id)
            chunk = page.read(_SCHEMA_SLOT_ID)
            if chunk is None:
                raise CorruptDatabaseError(
                    f"schema page {page_id} holds no descriptor chunk"
                )
            chunks.append(chunk)
            page_id = page.next_page_id
        if not chunks:
            raise CorruptDatabaseError(
                f"meta page points at schema page {first_page_id}, which is empty"
            )
        return b"".join(chunks)

    def _write_schema_chain(self, payload: bytes) -> int:
        """Split ``payload`` across as many SCHEMA pages as it needs.

        A descriptor can exceed one page — a wide table with long column names
        easily does, and the test suite's 256-byte pages make it the common
        case. Chaining pages here is a miniature of the overflow-page technique
        SQLite uses for oversized cells, applied to one specific structure
        rather than to records in general.
        """
        pages: list[Page] = []
        offset = 0
        while True:
            page = self._pager.allocate_page(PageType.SCHEMA)
            chunk = payload[offset : offset + page.max_payload_size]
            page.insert(chunk)
            pages.append(page)
            offset += len(chunk)
            if offset >= len(payload):
                break

        for current, following in itertools.pairwise(pages):
            current.next_page_id = following.page_id
        for page in pages:
            self._pager.write_page(page)
        return pages[0].page_id

    def schema_page_ids(self) -> frozenset[int]:
        """Page ids holding the table descriptor."""
        meta = self._pager.meta
        if meta.schema_page_id == INVALID_PAGE_ID:
            return frozenset()
        page_ids: set[int] = set()
        page_id = meta.schema_page_id
        while page_id != INVALID_PAGE_ID and page_id not in page_ids:
            page_ids.add(page_id)
            page_id = self._pager.read_page(page_id).next_page_id
        return frozenset(page_ids)

    def _on_heap_pages_changed(self, first_page_id: int, last_page_id: int) -> None:
        """Persist the heap's endpoints whenever the chain is extended."""
        meta = self._pager.meta
        meta.heap_first_page = first_page_id
        meta.heap_last_page = last_page_id
        self._pager.flush_meta()

    # -- schema ------------------------------------------------------------

    def create_table(self, name: str, schema: Schema) -> TableDescriptor:
        """Define this database's table.

        Milestone 1 allows exactly one. The SQL spelling of this call arrives
        with the parser in Milestone 2, and multiple tables with the catalog in
        Milestone 4.
        """
        self._ensure_open()
        if self._descriptor is not None:
            raise SchemaError(
                f"database already holds table {self._descriptor.name!r}. "
                f"Milestone 1 supports one table per file; multiple tables "
                f"arrive with the catalog in Milestone 4."
            )

        descriptor = TableDescriptor(name=name, schema=schema)
        schema_page_id = self._write_schema_chain(descriptor.to_json())

        # Create the heap before publishing the schema pointer, so a crash
        # between the two leaves an orphaned page rather than a table whose
        # heap does not exist. Making this genuinely atomic needs the WAL.
        self._heap = HeapFile.create(
            self._pager,
            on_pages_changed=self._on_heap_pages_changed,
            tracer=self._tracer,
        )
        self._pager.meta.schema_page_id = schema_page_id
        self._pager.flush_meta()
        self._pager.sync()

        self._descriptor = descriptor
        return descriptor

    @property
    def table(self) -> TableDescriptor | None:
        """The table descriptor, or ``None`` before :meth:`create_table`."""
        return self._descriptor

    @property
    def schema(self) -> Schema:
        return self._require_table().schema

    def _require_table(self) -> TableDescriptor:
        self._ensure_open()
        if self._descriptor is None:
            raise SchemaError(
                "no table defined yet; call create_table(name, schema) first"
            )
        return self._descriptor

    def _require_heap(self) -> HeapFile:
        self._require_table()
        assert self._heap is not None  # guaranteed by create_table / _load
        return self._heap

    # -- rows --------------------------------------------------------------

    def insert(self, values: Sequence[Any]) -> RecordId:
        """Encode ``values`` and append them to the heap."""
        schema = self._require_table().schema
        return self._require_heap().insert(encode_record(schema, values))

    def insert_many(self, rows: Sequence[Sequence[Any]]) -> list[RecordId]:
        """Insert several rows. Still one page write per row in Milestone 1.

        A real bulk loader fills a page in memory and writes it once; that
        becomes possible when the buffer pool lands in Milestone 7.
        """
        schema = self._require_table().schema
        heap = self._require_heap()
        return [heap.insert(encode_record(schema, row)) for row in rows]

    def get(self, record_id: RecordId) -> Row:
        """Fetch and decode one row by its physical address."""
        schema = self._require_table().schema
        return decode_record(schema, self._require_heap().get(record_id))

    def describe(self, record_id: RecordId) -> RecordLayout:
        """Fetch one row along with each column's byte range."""
        schema = self._require_table().schema
        return describe_record(schema, self._require_heap().get(record_id))

    def scan(self) -> Iterator[tuple[RecordId, Row]]:
        """Yield every live row, lazily, in physical order.

        Physical order is *not* insertion order after deletes: a tombstoned
        slot can be reused by a later row. Heaps are unordered by definition,
        which is why ``SELECT`` without ``ORDER BY`` guarantees nothing.
        """
        schema = self._require_table().schema
        for record_id, payload in self._require_heap().scan():
            yield record_id, decode_record(schema, payload)

    def rows(self) -> list[Row]:
        """Materialise every row. Convenience for tests and small tables."""
        return [row for _, row in self.scan()]

    def delete(self, record_id: RecordId) -> bool:
        """Tombstone one row. Returns ``False`` if it was already gone."""
        return self._require_heap().delete(record_id)

    def count(self) -> int:
        """Live row count. O(pages) — there is no cached count yet."""
        return self._require_heap().count()

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
    def heap(self) -> HeapFile | None:
        """The table's heap, or ``None`` before a table exists.

        Exposed for the executor's sequential scan. Milestone 4's catalog
        replaces this with a per-table lookup.
        """
        return self._heap

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

    def heap_page_ids(self) -> frozenset[int]:
        """Page ids belonging to the table's heap."""
        if self._heap is None:
            return frozenset()
        return frozenset(self._heap.page_ids())

    def page_summaries(self) -> list[PageSummary]:
        """Summarize every page in the file, in page order."""
        self._ensure_open()
        return iter_page_summaries(
            self._pager,
            range(self._pager.page_count),
            heap_pages=self.heap_page_ids(),
            schema_pages=self.schema_page_ids(),
            table_name=self._descriptor.name if self._descriptor else None,
        )

    def page_detail(self, page_id: int) -> PageDetail:
        """Fully inspect one page, decoding its records where possible."""
        self._ensure_open()
        return inspect_page(
            self._pager,
            page_id,
            schema=self._descriptor.schema if self._descriptor else None,
            heap_pages=self.heap_page_ids(),
            schema_pages=self.schema_page_ids(),
            table_name=self._descriptor.name if self._descriptor else None,
        )

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
        table = self._descriptor.name if self._descriptor else "no table"
        state = "closed" if self._closed else "open"
        return (
            f"<Database {self._database_id!r} {state} "
            f"table={table} pages={self._pager.page_count}>"
        )
