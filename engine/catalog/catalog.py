"""The catalog: what tables exist, and where they live.

Replaces Milestone 1's single JSON schema page.  Two things change as a result:
a database can hold many tables, and adding one is an ``INSERT`` into a system
table rather than a change to the file format.

    catalog.create_table("users", schema)
        │
        ├─ allocate a heap for the new table          → first_page
        ├─ INSERT (table_id, "users", first, last)    → chendb_tables
        └─ INSERT one row per column                  → chendb_columns

    catalog.get_table("users")
        │
        ├─ scan chendb_tables for name = "users"       → table_id, pages
        └─ scan chendb_columns for that table_id       → rebuild the Schema

Both lookups are full scans of the catalog. That is O(tables) and O(columns), so
a database with thousands of tables would notice — which is exactly why real
systems index their catalogs (PostgreSQL has ``pg_class_relname_nsp_index``) and
cache the results aggressively. ChenDB caches in memory per open database, so the
scans happen once; Milestone 5 makes a real index possible.

Cache coherence is the risk a cache always brings. It is handled by having
exactly one writer: every mutation goes through this class, which updates the
cache in the same call that writes the rows. Nothing else may write the system
tables — enforced by :func:`~engine.catalog.system.is_system_table` rejecting
them at the SQL layer.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.catalog.system import (
    COLUMNS_TABLE_ID,
    COLUMNS_TABLE_NAME,
    COLUMNS_TABLE_SCHEMA,
    FIRST_USER_TABLE_ID,
    TABLES_TABLE_ID,
    TABLES_TABLE_NAME,
    TABLES_TABLE_SCHEMA,
    is_system_table,
)
from engine.diagnostics.events import CatalogLookupEvent, TableCreatedEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import CatalogError, CorruptDatabaseError
from engine.serialization.record import decode_record, encode_record
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.storage.constants import INVALID_PAGE_ID
from engine.storage.heap import HeapFile

if TYPE_CHECKING:
    from engine.storage.pager import Pager

__all__ = ["Catalog", "TableInfo"]


@dataclass(frozen=True, slots=True)
class TableInfo:
    """Everything the engine knows about one table."""

    table_id: int
    name: str
    schema: Schema
    first_page: int
    last_page: int
    is_system: bool = False

    @property
    def column_count(self) -> int:
        return len(self.schema)


@dataclass(slots=True)
class _CacheEntry:
    info: TableInfo
    heap: HeapFile | None = None


@dataclass(slots=True)
class CatalogStats:
    """How much work the catalog has done. Makes the cache's value visible."""

    lookups: int = 0
    cache_hits: int = 0
    scans: int = 0
    tables_created: int = 0

    @property
    def hit_rate(self) -> float:
        return self.cache_hits / self.lookups if self.lookups else 0.0


class Catalog:
    """Reads and writes the system tables for one open database."""

    __slots__ = ("_cache", "_columns_heap", "_pager", "_stats", "_tables_heap", "_tracer")

    def __init__(self, pager: Pager, *, tracer: Tracer | None = None) -> None:
        self._pager = pager
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._cache: dict[str, _CacheEntry] = {}
        self._stats = CatalogStats()
        self._tables_heap: HeapFile | None = None
        self._columns_heap: HeapFile | None = None

    # -- bootstrap ---------------------------------------------------------

    @property
    def initialised(self) -> bool:
        return self._pager.meta.catalog_tables_first != INVALID_PAGE_ID

    def bootstrap(self) -> None:
        """Create the two system-table heaps in a brand-new database.

        Called once, at creation. The order matters only in that the meta page is
        written last: a crash before that leaves two orphaned pages rather than a
        meta page pointing at heaps that do not exist. Making it genuinely atomic
        needs the WAL in Milestone 9.
        """
        if self.initialised:
            raise CatalogError("catalog is already initialised")

        meta = self._pager.meta
        tables_heap = HeapFile.create(self._pager, tracer=self._tracer)
        columns_heap = HeapFile.create(self._pager, tracer=self._tracer)

        meta.catalog_tables_first = tables_heap.first_page_id
        meta.catalog_tables_last = tables_heap.last_page_id
        meta.catalog_columns_first = columns_heap.first_page_id
        meta.catalog_columns_last = columns_heap.last_page_id
        meta.next_table_id = FIRST_USER_TABLE_ID
        self._pager.flush_meta()
        self._pager.sync()

        self._tables_heap = tables_heap
        self._columns_heap = columns_heap

    def _require_bootstrap(self) -> None:
        if not self.initialised:
            raise CorruptDatabaseError(
                "the catalog is not initialised: this file was created by an "
                "older format version, or is damaged"
            )

    @property
    def tables_heap(self) -> HeapFile:
        """The heap holding ``chendb_tables``."""
        self._require_bootstrap()
        if self._tables_heap is None:
            meta = self._pager.meta
            self._tables_heap = HeapFile(
                self._pager,
                meta.catalog_tables_first,
                meta.catalog_tables_last,
                on_pages_changed=self._on_tables_pages_changed,
                tracer=self._tracer,
            )
        return self._tables_heap

    @property
    def columns_heap(self) -> HeapFile:
        """The heap holding ``chendb_columns``."""
        self._require_bootstrap()
        if self._columns_heap is None:
            meta = self._pager.meta
            self._columns_heap = HeapFile(
                self._pager,
                meta.catalog_columns_first,
                meta.catalog_columns_last,
                on_pages_changed=self._on_columns_pages_changed,
                tracer=self._tracer,
            )
        return self._columns_heap

    def _on_tables_pages_changed(self, first: int, last: int) -> None:
        self._pager.meta.catalog_tables_first = first
        self._pager.meta.catalog_tables_last = last
        self._pager.flush_meta()

    def _on_columns_pages_changed(self, first: int, last: int) -> None:
        self._pager.meta.catalog_columns_first = first
        self._pager.meta.catalog_columns_last = last
        self._pager.flush_meta()

    # -- reading -----------------------------------------------------------

    def _system_table_info(self, name: str) -> TableInfo | None:
        """Synthesise a system table's descriptor.

        Their schemas are compiled in and their page pointers live in the meta
        page, so there is no row to read — and therefore no way for a row and a
        bootstrap pointer to disagree.
        """
        meta = self._pager.meta
        folded = name.casefold()
        if folded == TABLES_TABLE_NAME:
            return TableInfo(
                table_id=TABLES_TABLE_ID,
                name=TABLES_TABLE_NAME,
                schema=TABLES_TABLE_SCHEMA,
                first_page=meta.catalog_tables_first,
                last_page=meta.catalog_tables_last,
                is_system=True,
            )
        if folded == COLUMNS_TABLE_NAME:
            return TableInfo(
                table_id=COLUMNS_TABLE_ID,
                name=COLUMNS_TABLE_NAME,
                schema=COLUMNS_TABLE_SCHEMA,
                first_page=meta.catalog_columns_first,
                last_page=meta.catalog_columns_last,
                is_system=True,
            )
        return None

    def get_table(self, name: str) -> TableInfo | None:
        """Look up a table by name, case-insensitively. ``None`` if absent."""
        self._require_bootstrap()
        self._stats.lookups += 1
        key = name.casefold()

        cached = self._cache.get(key)
        if cached is not None:
            self._stats.cache_hits += 1
            self._emit_lookup(name, found=True, cached=True)
            return cached.info

        system = self._system_table_info(name)
        if system is not None:
            self._cache[key] = _CacheEntry(system)
            self._emit_lookup(name, found=True, cached=False)
            return system

        info = self._scan_for_table(key)
        if info is None:
            self._emit_lookup(name, found=False, cached=False)
            return None

        self._cache[key] = _CacheEntry(info)
        self._emit_lookup(name, found=True, cached=False)
        return info

    def require_table(self, name: str) -> TableInfo:
        """Look up a table, raising if it does not exist."""
        info = self.get_table(name)
        if info is None:
            known = ", ".join(table.name for table in self.list_tables()) or "none"
            raise CatalogError(f"no table named {name!r}; this database has {known}")
        return info

    def _scan_for_table(self, folded_name: str) -> TableInfo | None:
        """Full scan of ``chendb_tables``. O(tables); the cache makes it once."""
        self._stats.scans += 1
        for _, payload in self.tables_heap.scan():
            row = decode_record(TABLES_TABLE_SCHEMA, payload)
            table_id, name, first_page, last_page = row
            if str(name).casefold() != folded_name:
                continue
            return TableInfo(
                table_id=int(table_id),  # type: ignore[arg-type]
                name=str(name),
                schema=self._load_schema(int(table_id)),  # type: ignore[arg-type]
                first_page=int(first_page),  # type: ignore[arg-type]
                last_page=int(last_page),  # type: ignore[arg-type]
            )
        return None

    def _load_schema(self, table_id: int) -> Schema:
        """Rebuild a table's schema from its ``chendb_columns`` rows."""
        self._stats.scans += 1
        found: list[tuple[int, Column]] = []
        for _, payload in self.columns_heap.scan():
            row = decode_record(COLUMNS_TABLE_SCHEMA, payload)
            owner, position, name, type_id, nullable, primary_key = row
            if int(owner) != table_id:  # type: ignore[arg-type]
                continue
            found.append(
                (
                    int(position),  # type: ignore[arg-type]
                    Column(
                        name=str(name),
                        data_type=DataType(int(type_id)),  # type: ignore[arg-type]
                        nullable=bool(nullable),
                        primary_key=bool(primary_key),
                    ),
                )
            )

        if not found:
            raise CorruptDatabaseError(
                f"table {table_id} has a chendb_tables row but no columns"
            )

        # Sorted by position, because a heap scan returns physical order and
        # column order is what determines the record layout.
        found.sort(key=lambda entry: entry[0])
        positions = [position for position, _ in found]
        if positions != list(range(len(positions))):
            raise CorruptDatabaseError(
                f"table {table_id} has non-contiguous column positions {positions}"
            )
        return Schema(tuple(column for _, column in found))

    def list_tables(self, *, include_system: bool = False) -> list[TableInfo]:
        """Every table, user tables first, each sorted by name."""
        self._require_bootstrap()
        self._stats.scans += 1

        user: list[TableInfo] = []
        for _, payload in self.tables_heap.scan():
            table_id, name, first_page, last_page = decode_record(
                TABLES_TABLE_SCHEMA, payload
            )
            key = str(name).casefold()
            cached = self._cache.get(key)
            if cached is not None:
                user.append(cached.info)
                continue
            info = TableInfo(
                table_id=int(table_id),  # type: ignore[arg-type]
                name=str(name),
                schema=self._load_schema(int(table_id)),  # type: ignore[arg-type]
                first_page=int(first_page),  # type: ignore[arg-type]
                last_page=int(last_page),  # type: ignore[arg-type]
            )
            self._cache[key] = _CacheEntry(info)
            user.append(info)

        user.sort(key=lambda info: info.name.casefold())
        if not include_system:
            return user

        system = [
            self._system_table_info(TABLES_TABLE_NAME),
            self._system_table_info(COLUMNS_TABLE_NAME),
        ]
        return user + [info for info in system if info is not None]

    def table_names(self) -> list[str]:
        return [info.name for info in self.list_tables()]

    # -- writing -----------------------------------------------------------

    def create_table(self, name: str, schema: Schema) -> TableInfo:
        """Allocate a heap for a new table and record it in the catalog."""
        self._require_bootstrap()

        if is_system_table(name):
            raise CatalogError(
                f"{name!r} is reserved: names beginning {'chendb_'!r} belong to "
                f"the engine"
            )
        if self.get_table(name) is not None:
            raise CatalogError(f"table {name!r} already exists")

        meta = self._pager.meta
        table_id = meta.next_table_id
        meta.next_table_id += 1

        heap = HeapFile.create(self._pager, tracer=self._tracer)
        info = TableInfo(
            table_id=table_id,
            name=name,
            schema=schema,
            first_page=heap.first_page_id,
            last_page=heap.last_page_id,
        )

        self.tables_heap.insert(
            encode_record(
                TABLES_TABLE_SCHEMA,
                (table_id, name, heap.first_page_id, heap.last_page_id),
            )
        )
        for position, column in enumerate(schema):
            self.columns_heap.insert(
                encode_record(
                    COLUMNS_TABLE_SCHEMA,
                    (
                        table_id,
                        position,
                        column.name,
                        int(column.data_type),
                        column.nullable,
                        column.primary_key,
                    ),
                )
            )

        self._pager.flush_meta()
        self._pager.sync()

        # Cache the heap alongside the info: it was just built, and rebuilding it
        # would mean re-reading the pages we already have.
        self._cache[name.casefold()] = _CacheEntry(info, heap)
        heap_on_change = self._page_updater(info)
        heap._on_pages_changed = heap_on_change

        self._stats.tables_created += 1
        if self._tracer.summary:
            self._tracer.emit(
                TableCreatedEvent(
                    table_name=name,
                    table_id=table_id,
                    column_count=len(schema),
                    first_page=heap.first_page_id,
                )
            )
        return info

    def heap_for(self, name: str) -> HeapFile:
        """The heap holding ``name``'s rows, opening it if necessary."""
        info = self.require_table(name)
        entry = self._cache[info.name.casefold()]
        if entry.heap is None:
            entry.heap = HeapFile(
                self._pager,
                info.first_page,
                info.last_page,
                on_pages_changed=self._page_updater(info),
                tracer=self._tracer,
            )
        return entry.heap

    def _page_updater(self, info: TableInfo):
        """A callback that writes a table's new page range back to the catalog.

        A heap extends itself when a page fills, and the catalog row recording
        ``last_page`` must follow — otherwise a reopened database would start its
        appends in the middle of the chain. Set on the heap after construction
        because the callback needs the ``TableInfo`` the heap is being built for.
        """

        def update(first: int, last: int) -> None:
            if info.is_system:
                return  # system heaps update the meta page instead
            self._update_table_pages(info, first, last)

        return update

    def _update_table_pages(self, info: TableInfo, first: int, last: int) -> None:
        """Rewrite the ``chendb_tables`` row for ``info`` with new page bounds.

        Delete-then-insert rather than update-in-place: the heap has no update
        operation, because a row that grows may not fit where it was. The row is
        fixed-width here, so an in-place update would work — but adding one to the
        heap for the catalog's sole benefit would be a special case that the
        general update in a later milestone has to unpick.
        """
        heap = self.tables_heap
        for record_id, payload in list(heap.scan()):
            row = decode_record(TABLES_TABLE_SCHEMA, payload)
            if int(row[0]) != info.table_id:  # type: ignore[arg-type]
                continue
            heap.delete(record_id)
            heap.insert(
                encode_record(
                    TABLES_TABLE_SCHEMA, (info.table_id, info.name, first, last)
                )
            )
            break

        updated = TableInfo(
            table_id=info.table_id,
            name=info.name,
            schema=info.schema,
            first_page=first,
            last_page=last,
            is_system=info.is_system,
        )
        entry = self._cache.get(info.name.casefold())
        if entry is not None:
            entry.info = updated

    # -- diagnostics -------------------------------------------------------

    @property
    def stats(self) -> CatalogStats:
        return self._stats

    def invalidate(self) -> None:
        """Drop the cache. For tests that mutate the catalog behind its back."""
        self._cache.clear()

    def _emit_lookup(self, name: str, *, found: bool, cached: bool) -> None:
        if not self._tracer.storage:
            return
        self._tracer.emit(
            CatalogLookupEvent(
                object_type="table", name=name, found=found, from_cache=cached
            )
        )

    def __iter__(self) -> Iterator[TableInfo]:
        return iter(self.list_tables())

    def __repr__(self) -> str:
        return f"<Catalog tables={len(self._cache)} cached stats={self._stats}>"
