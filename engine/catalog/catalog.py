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
    FIRST_USER_OBJECT_ID,
    INDEXES_TABLE_ID,
    INDEXES_TABLE_NAME,
    INDEXES_TABLE_SCHEMA,
    TABLES_TABLE_ID,
    TABLES_TABLE_NAME,
    TABLES_TABLE_SCHEMA,
    is_system_table,
)
from engine.diagnostics.events import (
    CatalogLookupEvent,
    IndexCreatedEvent,
    TableCreatedEvent,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import CatalogError, CorruptDatabaseError, SchemaError
from engine.index.bplustree import BPlusTree
from engine.index.key import encode_key
from engine.serialization.record import decode_record, encode_record
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.storage.constants import INVALID_PAGE_ID
from engine.storage.heap import HeapFile

if TYPE_CHECKING:
    from engine.storage.pager import Pager

__all__ = ["Catalog", "IndexInfo", "TableInfo"]


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


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """Everything the engine knows about one index."""

    index_id: int
    name: str
    table_name: str
    table_id: int
    column_position: int
    column_name: str
    data_type: DataType
    root_page: int
    unique: bool

    def encode(self, value: object) -> bytes:
        """Encode one value as a search key for this index."""
        return encode_key(value, self.data_type)


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
    indexes_created: int = 0

    @property
    def hit_rate(self) -> float:
        return self.cache_hits / self.lookups if self.lookups else 0.0


class Catalog:
    """Reads and writes the system tables for one open database."""

    __slots__ = (
        "_cache",
        "_columns_heap",
        "_index_cache",
        "_indexes_heap",
        "_pager",
        "_stats",
        "_tables_heap",
        "_tracer",
        "_trees",
    )

    def __init__(self, pager: Pager, *, tracer: Tracer | None = None) -> None:
        self._pager = pager
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._cache: dict[str, _CacheEntry] = {}
        self._index_cache: dict[str, IndexInfo] | None = None
        self._trees: dict[str, BPlusTree] = {}
        self._stats = CatalogStats()
        self._tables_heap: HeapFile | None = None
        self._columns_heap: HeapFile | None = None
        self._indexes_heap: HeapFile | None = None

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
        indexes_heap = HeapFile.create(self._pager, tracer=self._tracer)

        meta.catalog_tables_first = tables_heap.first_page_id
        meta.catalog_tables_last = tables_heap.last_page_id
        meta.catalog_columns_first = columns_heap.first_page_id
        meta.catalog_columns_last = columns_heap.last_page_id
        meta.catalog_indexes_first = indexes_heap.first_page_id
        meta.catalog_indexes_last = indexes_heap.last_page_id
        meta.next_object_id = FIRST_USER_OBJECT_ID
        self._pager.flush_meta()
        self._pager.sync()

        self._tables_heap = tables_heap
        self._columns_heap = columns_heap
        self._indexes_heap = indexes_heap

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

    @property
    def indexes_heap(self) -> HeapFile:
        """The heap holding ``chendb_indexes``."""
        self._require_bootstrap()
        if self._indexes_heap is None:
            meta = self._pager.meta
            self._indexes_heap = HeapFile(
                self._pager,
                meta.catalog_indexes_first,
                meta.catalog_indexes_last,
                on_pages_changed=self._on_indexes_pages_changed,
                tracer=self._tracer,
            )
        return self._indexes_heap

    def _on_columns_pages_changed(self, first: int, last: int) -> None:
        self._pager.meta.catalog_columns_first = first
        self._pager.meta.catalog_columns_last = last
        self._pager.flush_meta()

    def _on_indexes_pages_changed(self, first: int, last: int) -> None:
        self._pager.meta.catalog_indexes_first = first
        self._pager.meta.catalog_indexes_last = last
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
        if folded == INDEXES_TABLE_NAME:
            return TableInfo(
                table_id=INDEXES_TABLE_ID,
                name=INDEXES_TABLE_NAME,
                schema=INDEXES_TABLE_SCHEMA,
                first_page=meta.catalog_indexes_first,
                last_page=meta.catalog_indexes_last,
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
            self._system_table_info(INDEXES_TABLE_NAME),
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
        table_id = meta.next_object_id
        meta.next_object_id += 1

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

    # -- indexes -----------------------------------------------------------

    def list_indexes(self, table: str | None = None) -> list[IndexInfo]:
        """Every index, optionally narrowed to one table, sorted by name."""
        self._require_bootstrap()
        indexes = list(self._index_map().values())
        if table is not None:
            folded = self.require_table(table).name.casefold()
            indexes = [
                index for index in indexes if index.table_name.casefold() == folded
            ]
        return sorted(indexes, key=lambda index: index.name.casefold())

    def get_index(self, name: str) -> IndexInfo | None:
        self._require_bootstrap()
        self._stats.lookups += 1
        index = self._index_map().get(name.casefold())
        if index is not None:
            self._stats.cache_hits += 1
        self._emit_lookup(name, found=index is not None, cached=index is not None, kind="index")
        return index

    def require_index(self, name: str) -> IndexInfo:
        index = self.get_index(name)
        if index is None:
            known = ", ".join(i.name for i in self.list_indexes()) or "none"
            raise CatalogError(f"no index named {name!r}; this database has {known}")
        return index

    def indexes_on(self, table: str, column_position: int) -> list[IndexInfo]:
        """Indexes whose key is exactly ``column_position`` of ``table``.

        What the planner asks: "is there an access path for this column?".
        Returns a list because nothing forbids two indexes on the same column,
        and unique ones sort first so a planner preferring them needs no
        second pass.
        """
        matches = [
            index
            for index in self.list_indexes(table)
            if index.column_position == column_position
        ]
        matches.sort(key=lambda index: (not index.unique, index.name.casefold()))
        return matches

    def _index_map(self) -> dict[str, IndexInfo]:
        """Load and cache every index row.

        Cached whole rather than per name: ``chendb_indexes`` holds one row per
        index and a database has few, so one scan answers every question about
        them — including the planner's "any index on this column?", which a
        by-name cache could not answer without scanning anyway.
        """
        if self._index_cache is not None:
            return self._index_cache

        self._stats.scans += 1
        tables_by_id = {
            info.table_id: info for info in self.list_tables(include_system=True)
        }
        found: dict[str, IndexInfo] = {}
        for _, payload in self.indexes_heap.scan():
            row = decode_record(INDEXES_TABLE_SCHEMA, payload)
            index_id, table_id, name, position, root_page, is_unique = row
            owner = tables_by_id.get(int(table_id))  # type: ignore[arg-type]
            if owner is None:
                raise CorruptDatabaseError(
                    f"index {name!r} references table {table_id}, which does not exist"
                )
            if not 0 <= int(position) < len(owner.schema):  # type: ignore[arg-type]
                raise CorruptDatabaseError(
                    f"index {name!r} references column {position} of "
                    f"{owner.name!r}, which has {len(owner.schema)} columns"
                )
            column = owner.schema[int(position)]  # type: ignore[arg-type]
            found[str(name).casefold()] = IndexInfo(
                index_id=int(index_id),  # type: ignore[arg-type]
                name=str(name),
                table_name=owner.name,
                table_id=owner.table_id,
                column_position=int(position),  # type: ignore[arg-type]
                column_name=column.name,
                data_type=column.data_type,
                root_page=int(root_page),  # type: ignore[arg-type]
                unique=bool(is_unique),
            )
        self._index_cache = found
        return found

    def create_index(
        self, name: str, table: str, column: str, *, unique: bool = False
    ) -> IndexInfo:
        """Create an index and populate it from the table's existing rows.

        Population is row by row through :meth:`BPlusTree.insert`, so building an
        index over *n* rows costs O(n log n) and a split roughly every half-node.
        A real system sorts first and packs leaves in one pass; see
        :mod:`engine.index.bplustree` on bulk loading.

        A ``UNIQUE`` index over a column that already has duplicates fails here,
        after some entries are in the tree.  Without transactions there is no way
        to unwind that cleanly, so the partially built tree is dropped from the
        catalog cache and its pages are simply abandoned — the same leak
        Milestone 9's WAL exists to close.
        """
        self._require_bootstrap()

        if is_system_table(name):
            raise CatalogError(
                f"{name!r} is reserved: names beginning {'chendb_'!r} belong to the engine"
            )
        if self.get_index(name) is not None:
            raise CatalogError(f"index {name!r} already exists")
        if self.get_table(name) is not None:
            raise CatalogError(f"{name!r} is already a table name")

        info = self.require_table(table)
        if info.is_system:
            raise CatalogError(f"cannot index the system table {info.name!r}")
        try:
            position = info.schema.index_of(column)
        except SchemaError:
            raise CatalogError(
                f"no column named {column!r} in {info.name!r}; it has "
                f"{', '.join(info.schema.column_names)}"
            ) from None

        meta = self._pager.meta
        index_id = meta.next_object_id
        meta.next_object_id += 1

        tree = BPlusTree.create(
            self._pager,
            name=name,
            data_type=info.schema[position].data_type,
            unique=unique,
            tracer=self._tracer,
        )

        rows_indexed = 0
        heap = self.heap_for(info.name)
        for record_id, payload in heap.scan():
            values = decode_record(info.schema, payload)
            tree.insert(encode_key(values[position], info.schema[position].data_type), record_id)
            rows_indexed += 1

        self.indexes_heap.insert(
            encode_record(
                INDEXES_TABLE_SCHEMA,
                (index_id, info.table_id, name, position, tree.root_page_id, unique),
            )
        )
        self._pager.flush_meta()
        self._pager.sync()

        # A root split during population moved the root, so the row above already
        # carries the final page id. Dropping the cache is still needed: it was
        # loaded before this index existed.
        self._index_cache = None
        index = self.require_index(name)
        tree._on_root_changed = self._root_updater(index)
        self._trees[name.casefold()] = tree

        self._stats.indexes_created += 1
        if self._tracer.summary:
            self._tracer.emit(
                IndexCreatedEvent(
                    index_name=name,
                    index_id=index_id,
                    table_name=info.name,
                    column_name=index.column_name,
                    unique=unique,
                    rows_indexed=rows_indexed,
                    root_page=tree.root_page_id,
                )
            )
        return index

    def tree_for(self, name: str) -> BPlusTree:
        """Open (or reuse) the B+ tree behind an index."""
        index = self.require_index(name)
        key = index.name.casefold()
        tree = self._trees.get(key)
        if tree is None:
            tree = BPlusTree(
                self._pager,
                index.root_page,
                name=index.name,
                data_type=index.data_type,
                unique=index.unique,
                on_root_changed=self._root_updater(index),
                tracer=self._tracer,
            )
            self._trees[key] = tree
        return tree

    def _root_updater(self, index: IndexInfo):
        """Keep ``chendb_indexes.root_page`` following a root split.

        The exact shape of :meth:`_page_updater` for heaps, and needed for the
        same reason: the tree relocates its own root and the catalog is the only
        thing that remembers where it was. Miss this and the index still answers
        correctly until the database is closed, then comes back rooted at a page
        that is now an interior node — a bug that would only ever show up after a
        restart.
        """

        def update(root_page: int) -> None:
            self._update_index_root(index, root_page)

        return update

    def _update_index_root(self, index: IndexInfo, root_page: int) -> None:
        heap = self.indexes_heap
        for record_id, payload in list(heap.scan()):
            row = decode_record(INDEXES_TABLE_SCHEMA, payload)
            if int(row[0]) != index.index_id:  # type: ignore[arg-type]
                continue
            heap.delete(record_id)
            heap.insert(
                encode_record(
                    INDEXES_TABLE_SCHEMA,
                    (
                        index.index_id,
                        index.table_id,
                        index.name,
                        index.column_position,
                        root_page,
                        index.unique,
                    ),
                )
            )
            break
        if self._index_cache is not None:
            self._index_cache[index.name.casefold()] = IndexInfo(
                index_id=index.index_id,
                name=index.name,
                table_name=index.table_name,
                table_id=index.table_id,
                column_position=index.column_position,
                column_name=index.column_name,
                data_type=index.data_type,
                root_page=root_page,
                unique=index.unique,
            )

    def index_page_ids(self) -> dict[int, str]:
        """Which index each B+ tree page belongs to, for the disk map."""
        owners: dict[int, str] = {}
        for index in self.list_indexes():
            for page_id in self.tree_for(index.name).page_ids():
                owners[page_id] = index.name
        return owners

    # -- diagnostics -------------------------------------------------------

    @property
    def stats(self) -> CatalogStats:
        return self._stats

    def invalidate(self) -> None:
        """Drop the cache. For tests that mutate the catalog behind its back."""
        self._cache.clear()
        self._index_cache = None
        self._trees.clear()

    def _emit_lookup(
        self, name: str, *, found: bool, cached: bool, kind: str = "table"
    ) -> None:
        if not self._tracer.storage:
            return
        self._tracer.emit(
            CatalogLookupEvent(
                object_type=kind, name=name, found=found, from_cache=cached
            )
        )

    def __iter__(self) -> Iterator[TableInfo]:
        return iter(self.list_tables())

    def __repr__(self) -> str:
        return f"<Catalog tables={len(self._cache)} cached stats={self._stats}>"
