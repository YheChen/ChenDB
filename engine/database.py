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

Transactions, and which API gets them
-------------------------------------
:meth:`begin`, :meth:`commit` and :meth:`rollback` wrap a group of writes, and
:meth:`transaction` is the context-manager form::

    with db.transaction():
        db.insert("users", (1, "ada"))
        db.insert("users", (2, "alan"))

**The SQL layer opens one for you; these methods do not.**
:func:`~engine.executor.engine.execute_script` wraps every statement in an
implicit transaction, so ``INSERT INTO t VALUES (a), (b), (c)`` that fails on
``c`` leaves none of them. A bare :meth:`insert` from Python is a single write
with nobody watching — atomic because it is one write, but not committed, and
therefore **not durable until something commits or syncs**.

That is deliberate rather than an oversight. Wrapping every embedded call would
mean an ``fsync`` per call once Milestone 9's log arrived, turning a 19 µs
insert into a 90 µs one, and the embedded API's whole reason to exist is to be
the fast path. The SQL layer is where autocommit belongs, because that is where
the statement boundary is.

``CREATE TABLE`` — several rows across two system tables — became atomic for
free in Milestone 8, because the undo log works in pages and does not care what
the rows meant.

Durability
----------
Since Milestone 9, **committing is what makes work durable**, not :meth:`sync`.
A commit appends a record to the write-ahead log and ``fsync``s *the log*; the
pages may still be sitting in the buffer pool, and recovery on the next open
puts them back. :meth:`sync` still forces everything to disk and is what a bare
embedded write needs, since it never commits.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from engine.catalog.catalog import Catalog, IndexInfo, TableInfo
from engine.diagnostics.events import DatabaseClosedEvent, DatabaseOpenedEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import (
    CatalogError,
    ChenDBError,
    RecordNotFoundError,
    TransactionError,
)
from engine.index.bplustree import BPlusTree
from engine.planner.statistics import StatisticsCatalog
from engine.serialization.record import (
    RecordLayout,
    Row,
    decode_record,
    describe_record,
    encode_record,
)
from engine.serialization.schema import Schema
from engine.storage.buffer import DEFAULT_POOL_FRAMES
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
from engine.transaction.manager import Transaction, TransactionManager

__all__ = ["DATABASE_SUFFIX", "Database"]

#: Conventional extension. Nothing enforces it; it just makes files obvious.
DATABASE_SUFFIX = ".chendb"


class Database:
    """A single ChenDB database file, holding any number of tables."""

    __slots__ = (
        "_catalog",
        "_closed",
        "_database_id",
        "_pager",
        "_statistics",
        "_tracer",
        "_transactions",
    )

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
        self._statistics = StatisticsCatalog(self, tracer=self._tracer)
        self._transactions = TransactionManager(tracer=self._tracer)
        # One hook, and every page change in the engine becomes undoable.
        pager.on_before_write = self._transactions.before_write
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
        buffer_pool_frames: int = DEFAULT_POOL_FRAMES,
    ) -> Database:
        """Open ``path``, creating and initialising it if it does not exist.

        ``buffer_pool_frames`` sizes the page cache. A deliberately small pool
        is the way to demonstrate eviction: with the default the whole of a
        teaching-sized database is resident and nothing is ever evicted.
        """
        resolved = Path(path)
        existed = resolved.exists() and resolved.stat().st_size > 0
        pager = Pager(
            resolved,
            page_size=page_size,
            create=create,
            verify_checksums=verify_checksums,
            tracer=tracer,
            buffer_pool_frames=buffer_pool_frames,
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

    # -- transactions ------------------------------------------------------

    @property
    def transactions(self) -> TransactionManager:
        """The transaction manager. Read it for the timeline."""
        return self._transactions

    @property
    def in_transaction(self) -> bool:
        return self._transactions.in_transaction

    def begin(self, *, implicit: bool = False) -> Transaction:
        """Open a transaction, so the writes that follow can be taken back."""
        self._ensure_open()
        return self._transactions.begin(implicit=implicit)

    def commit(self) -> Transaction:
        """Accept the work. Durability is still :meth:`sync`'s job.

        A ``COMMIT`` on a transaction that already had a statement fail rolls
        back instead. That is what PostgreSQL does — ``COMMIT`` in an aborted
        block prints ``ROLLBACK`` — and it is the safe direction: the caller
        never gets half a transaction, and never gets stuck in one either. The
        returned transaction reports ``aborted``, so nothing has to guess.
        """
        self._ensure_open()
        if self._transactions.is_failed:
            return self.rollback()
        transaction = self._transactions.commit()
        # After the manager, not before: a commit record for a transaction the
        # manager then refused would be a durable lie.
        self._pager.log_commit(transaction.transaction_id)
        return transaction

    def rollback(self) -> Transaction:
        """Put every page back as it was when the transaction started.

        Restoring bytes is only most of the job. Two pieces of engine state live
        in memory rather than on a page the engine re-reads, and both would
        otherwise survive a rollback and describe a database that no longer
        exists:

        * the **meta page** is a decoded dataclass, so ``page_count`` and
          ``next_object_id`` have to be re-read from the restored bytes;
        * the **catalog cache** and the **statistics** are derived from pages
          that just changed underneath them — a rolled-back ``CREATE TABLE``
          would otherwise leave the engine happily serving a table with no rows
          on disk.
        """
        self._ensure_open()
        active = self._transactions.active
        if active is not None and active.undo.overflowed:
            self._restore_from_wal(active.transaction_id)
        transaction = self._transactions.rollback(self._restore_page)
        self._pager.log_abort(transaction.transaction_id)
        self._pager.reload_meta()
        self._catalog.invalidate()
        self._statistics.invalidate()
        return transaction

    # -- durability --------------------------------------------------------

    @property
    def wal(self):
        """The write-ahead log, or None if this handle was opened without one."""
        return self._pager.wal

    @property
    def recovery(self):
        """What recovery did when this file was opened.

        ``recovery.ran`` is False after a clean shutdown, because a clean
        shutdown ends with a checkpoint and leaves an empty log. So it means
        exactly "the previous process did not close properly".
        """
        return self._pager.recovery

    def checkpoint(self) -> int:
        """Flush every dirty page and discard the log. Returns pages written.

        Refuses while a transaction is open, because discarding the log would
        discard that transaction's before-images — the ones a rollback past the
        in-memory cap reads, and the ones recovery would need if the machine
        died before the commit. Real systems solve this by keeping the log back
        to the oldest active transaction's first record instead of truncating
        wholesale; ChenDB's checkpoints are all-or-nothing, so refusing is the
        version of that rule this design can express.

        Nothing needs to call this: :meth:`close` does it, and a real system
        would run it on a timer or when the log outgrew a threshold. It is
        public because watching the log collapse to zero bytes is the clearest
        way to see what a checkpoint is for.
        """
        self._ensure_open()
        if self._transactions.in_transaction:
            raise TransactionError(
                "cannot checkpoint with a transaction open: truncating the log "
                "would discard the before-images that transaction needs to roll "
                "back. Commit or roll back first."
            )
        return self._pager.checkpoint()

    def _restore_from_wal(self, transaction_id: int) -> None:
        """Put back the pages the in-memory undo log could not hold.

        Only runs for a transaction that overflowed :data:`MAX_UNDO_BYTES`. The
        WAL carries a before-image for every page a transaction first touched —
        the same first-write-wins rule, on disk — so the log has exactly what
        memory dropped.

        Runs *before* the in-memory rollback rather than instead of it, so the
        cached images are applied last. They are the same bytes either way —
        this is only ever filling the gaps memory could not hold — and ordering
        it this way keeps the memory path the one that decides.

        The scan runs **forwards** and keeps the first image per page, not the
        last. There is only ever one, because first-write-wins is enforced on
        the decision rather than on the cache, but scanning forwards is what
        makes that a property of the code rather than a thing to remember.
        """
        log = self._pager.wal
        if log is None:
            raise TransactionError(
                "this transaction outgrew the in-memory undo log and there is "
                "no write-ahead log to fall back on; it cannot be rolled back"
            )
        log.flush()
        records, _ = log.read_all()
        restored: set[int] = set()
        for record in records:
            if record.transaction_id != transaction_id or not record.has_undo:
                continue
            if record.page_id in restored:
                continue
            restored.add(record.page_id)
            self._restore_page(record.page_id, record.before_image)

    def _restore_page(self, page_id: int, image: bytes) -> None:
        """Write one before-image back, bypassing the undo hook.

        Bypassing matters: the hook is what captures before-images, and a
        rollback writing through it would try to capture the page it is in the
        middle of restoring.
        """
        self._pager.restore_page(page_id, image)

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Run a block atomically: commit on success, roll back on anything else.

            with db.transaction():
                db.insert("users", (1, "ada"))
                db.insert("users", (2, "alan"))

        Nests by adopting an outer transaction rather than opening a second one
        — ChenDB has no savepoints, so an inner block cannot roll back
        independently, and pretending otherwise would be worse than not nesting.
        """
        outer = self._transactions.active
        transaction = self.begin(implicit=True) if outer is None else outer
        try:
            yield transaction
        except BaseException:
            if self._owns(outer, transaction):
                self.rollback()
            raise
        else:
            if self._owns(outer, transaction):
                self.commit()

    def _owns(self, outer: Transaction | None, transaction: Transaction) -> bool:
        """True when :meth:`transaction` should end ``transaction`` itself.

        Two ways it should not. The caller had one open already, so it decides;
        or the block ended this one itself — ``db.rollback()`` inside a ``with``
        is a reasonable thing to write, and the context manager committing over
        the top of it would raise "COMMIT with no transaction open" and hide
        whatever the block was doing.
        """
        return outer is None and self._transactions.active is transaction

    @property
    def statistics(self) -> StatisticsCatalog:
        """Table and column statistics, gathered on demand.

        In memory only, so they vanish on close — see
        :mod:`engine.planner.statistics` for why that is a choice rather than
        an omission.
        """
        return self._statistics

    def analyze(self, table: str | None = None) -> list:
        """Recompute statistics. What the ``ANALYZE`` statement runs."""
        self._ensure_open()
        if table is None:
            return self._statistics.gather_all()
        return [self._statistics.gather(table)]

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

    # -- indexes -----------------------------------------------------------

    def create_index(
        self, name: str, table: str, column: str, *, unique: bool = False
    ) -> IndexInfo:
        """Build a B+ tree over one column, populated from the existing rows."""
        self._ensure_open()
        index = self._catalog.create_index(name, table, column, unique=unique)
        self.sync()
        return index

    def index(self, name: str) -> IndexInfo | None:
        self._ensure_open()
        return self._catalog.get_index(name)

    def indexes(self, table: str | None = None) -> list[IndexInfo]:
        """Every index, optionally narrowed to one table."""
        self._ensure_open()
        return self._catalog.list_indexes(table)

    def tree_for(self, name: str) -> BPlusTree:
        """The B+ tree behind an index. For the executor and the tree view."""
        self._ensure_open()
        return self._catalog.tree_for(name)

    def lookup(self, index_name: str, value: Any) -> list[Row]:
        """Every row whose indexed column equals ``value``.

        Two steps, and the second is the one that costs: the tree gives record
        ids, then each row is fetched from the heap by address. Those fetches are
        scattered across the file, so an index that matches many rows can be
        *slower* than a sequential scan — the reason a real planner refuses an
        index once its estimated selectivity gets too high.
        """
        index = self._catalog.require_index(index_name)
        info = self.require_table(index.table_name)
        heap = self._catalog.heap_for(info.name)
        tree = self._catalog.tree_for(index.name)
        return [
            decode_record(info.schema, heap.get(record_id))
            for record_id in tree.search(index.encode(value))
        ]

    # -- rows --------------------------------------------------------------

    def insert(self, table: str, values: Sequence[Any]) -> RecordId:
        """Encode ``values`` and append them to ``table``, updating its indexes."""
        return self.insert_many(table, (values,))[0]

    def insert_many(self, table: str, rows: Sequence[Sequence[Any]]) -> list[RecordId]:
        """Insert several rows. Still one page write per row in Milestone 5.

        A real bulk loader fills a page in memory and writes it once; that becomes
        possible when the buffer pool lands in Milestone 7.

        Every index on the table is updated in the same call.  That is the cost
        indexes impose on writes and the reason they are not free: an insert into
        a table with three indexes is four B+ tree descents plus the heap write.
        Not atomic — a unique violation on the second index leaves the first one
        updated and the row in the heap — which is one more thing Milestone 9's
        write-ahead log is for.
        """
        info = self.require_table(table)
        heap = self._catalog.heap_for(info.name)
        indexes = self._catalog.list_indexes(info.name)

        record_ids: list[RecordId] = []
        for row in rows:
            record_id = heap.insert(encode_record(info.schema, row))
            record_ids.append(record_id)
            for index in indexes:
                self._catalog.tree_for(index.name).insert(
                    index.encode(row[index.column_position]), record_id
                )
        return record_ids

    def get(self, table: str, record_id: RecordId) -> Row:
        """Fetch and decode one row by its physical address."""
        info = self.require_table(table)
        return decode_record(info.schema, self._catalog.heap_for(info.name).get(record_id))

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
        """Tombstone one row and remove it from every index.

        The row has to be read before it is deleted: an index entry is keyed on
        the *value*, so removing it needs to know what that value was. This is
        why a delete costs a read even when the caller already knows the address,
        and why PostgreSQL instead leaves index entries pointing at dead tuples
        and cleans them up later in ``VACUUM``.
        """
        info = self.require_table(table)
        heap = self._catalog.heap_for(info.name)
        indexes = self._catalog.list_indexes(info.name)

        if indexes:
            try:
                values = decode_record(info.schema, heap.get(record_id))
            except RecordNotFoundError:
                return False
            for index in indexes:
                self._catalog.tree_for(index.name).delete(
                    index.encode(values[index.column_position]), record_id
                )
        return heap.delete(record_id)

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
        """Which table or index each page belongs to, for the disk map.

        Walks every heap chain and every tree, so it costs O(pages) reads. Only
        the inspector calls it.
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
        owners.update(self._catalog.index_page_ids())
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

    def abandon(self) -> None:
        """Simulate a crash: drop everything unwritten and release the file.

        **This loses data on purpose.** An open transaction is *not* rolled
        back, dirty pages are *not* flushed, and no checkpoint runs — so
        reopening the file goes through recovery, which is the point. Committed
        work survives, because its commit record was ``fsync``ed when it
        committed; everything else is gone.
        """
        if self._closed:
            return
        self._pager.abandon()
        self._closed = True

    def close(self) -> None:
        """Roll back anything unfinished, checkpoint, and release the file.

        Idempotent. An open transaction is **rolled back**, not committed: a
        handle closed with work outstanding is a handle whose caller never said
        the work was good, and keeping it would be the closest thing to a
        silent commit. It also has to happen before the checkpoint, which
        refuses to run while a transaction is open.
        """
        if self._closed:
            return
        if self._transactions.in_transaction:
            self.rollback()
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
