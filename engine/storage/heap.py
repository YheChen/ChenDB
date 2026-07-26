"""The heap file: an unordered collection of records spread over pages.

A heap is the default physical representation of a table.  Rows live wherever
they fit, in no particular order, and are addressed by
:class:`RecordId` — a ``(page_id, slot_id)`` pair.  PostgreSQL calls the same
thing a ``ctid``; SQLite has no heap at all, because every table there *is* a
B+ tree keyed by rowid.

Structure
---------
Pages are threaded together by the ``next_page_id`` field in each page
header::

    meta.heap_first_page                        meta.heap_last_page
            │                                            │
            ▼                                            ▼
        ┌───────┐      ┌───────┐      ┌───────┐      ┌───────┐
        │page 2 │─────▶│page 5 │─────▶│page 6 │─────▶│page 9 │──▶ ∅
        └───────┘      └───────┘      └───────┘      └───────┘

A linked list, rather than "pages *f* through *l*", because Milestone 4 puts
several tables plus a catalog in one file and their pages will interleave.

Why keep ``heap_last_page``?  Without it an append walks the whole chain,
making bulk loading O(n²) in pages.  With it, insert is O(1).

Insert policy
-------------
Try the last page; if the record does not fit, allocate a new page and link it
on.  That is deliberately the simplest policy that is still O(1), and it has a
real cost: space freed by deletes in earlier pages is never reused, so a
delete-heavy workload grows the file without bound.

Real systems keep a *free space map*: PostgreSQL maintains an FSM fork per
table — itself a tree of per-page free-space bytes — so an insert can find a
page with room in O(log pages). Milestone 7's buffer pool makes consulting such
a map cheap enough to be worth it.

Complexity
----------
=========================  =========================================
Operation                  Cost
=========================  =========================================
``insert``                 O(1) — one page read, one or two writes
``get(rid)``               O(1) — one page read
``delete(rid)``            O(1) — one read, one write
``scan``                   O(pages) reads, O(rows) work
``count``                  O(pages) — no cached row count in M1
=========================  =========================================

Every one of those page reads is a syscall until Milestone 7.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from engine.diagnostics.events import (
    HeapScanEvent,
    PageCompactedEvent,
    RecordDeletedEvent,
    RecordInsertedEvent,
    RecordReadEvent,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import RecordNotFoundError, RecordTooLargeError
from engine.storage.constants import INVALID_PAGE_ID, PageType
from engine.storage.page import Page
from engine.storage.pager import Pager

__all__ = ["HeapFile", "HeapStats", "RecordId"]


@dataclass(frozen=True, order=True, slots=True)
class RecordId:
    """Physical address of a record: which page, which slot.

    Stable across page compaction (the slot directory absorbs the move) but
    **not** across a row moving to a different page. Milestone 5's index will
    store these as leaf payloads, which is why an update that relocates a row
    has to touch every index — PostgreSQL's HOT optimisation exists to dodge
    exactly that cost.
    """

    page_id: int
    slot_id: int

    def __str__(self) -> str:
        return f"({self.page_id},{self.slot_id})"


@dataclass(slots=True)
class HeapStats:
    """Counters for one heap, reset when the handle is created."""

    inserts: int = 0
    deletes: int = 0
    reads: int = 0
    scans: int = 0
    pages_allocated: int = 0
    compactions: int = 0


class HeapFile:
    """A chain of :class:`~engine.storage.page.Page` objects holding records.

    The heap does not know where its first and last page ids are persisted.
    The owner supplies them and passes ``on_pages_changed``, which fires
    whenever the chain is extended.  That keeps the heap independent of the
    meta page in Milestone 1 and of the catalog in Milestone 4.
    """

    __slots__ = (
        "_first_page_id",
        "_last_page_id",
        "_on_pages_changed",
        "_pager",
        "_stats",
        "_tracer",
    )

    def __init__(
        self,
        pager: Pager,
        first_page_id: int,
        last_page_id: int,
        *,
        on_pages_changed: Callable[[int, int], None] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._pager = pager
        self._first_page_id = first_page_id
        self._last_page_id = last_page_id
        self._on_pages_changed = on_pages_changed
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._stats = HeapStats()

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        pager: Pager,
        *,
        on_pages_changed: Callable[[int, int], None] | None = None,
        tracer: Tracer | None = None,
    ) -> HeapFile:
        """Allocate the first page of a brand-new heap."""
        page = pager.allocate_page(PageType.HEAP)
        heap = cls(
            pager,
            page.page_id,
            page.page_id,
            on_pages_changed=on_pages_changed,
            tracer=tracer,
        )
        heap._stats.pages_allocated += 1
        if on_pages_changed is not None:
            on_pages_changed(page.page_id, page.page_id)
        return heap

    # -- properties --------------------------------------------------------

    @property
    def first_page_id(self) -> int:
        return self._first_page_id

    @property
    def last_page_id(self) -> int:
        return self._last_page_id

    @property
    def stats(self) -> HeapStats:
        return self._stats

    # -- writes ------------------------------------------------------------

    def insert(self, payload: bytes) -> RecordId:
        """Append ``payload`` and return its address.

        Tries the tail page first. A record too large for an empty page can
        never be stored and raises :class:`RecordTooLargeError`.
        """
        page = self._pager.read_page(self._last_page_id)
        if len(payload) > page.max_payload_size:
            raise RecordTooLargeError(
                f"record of {len(payload)} bytes exceeds the "
                f"{page.max_payload_size}-byte maximum for a "
                f"{self._pager.page_size}-byte page. Real systems store "
                f"oversized values out of line (PostgreSQL TOAST, SQLite "
                f"overflow pages); ChenDB does not yet."
            )

        slot_id = page.insert(payload)
        if slot_id is None and page.would_fit_after_compaction(len(payload)):
            # Tombstones are hiding enough room. Compacting one page we have
            # already read is far cheaper than allocating another page and
            # paying to read it on every future scan.
            reclaimed = page.compact()
            self._stats.compactions += 1
            if self._tracer.storage:
                self._tracer.emit(
                    PageCompactedEvent(
                        page_id=page.page_id, reclaimed_bytes=reclaimed
                    )
                )
            slot_id = page.insert(payload)

        if slot_id is None:
            page = self._extend(page)
            slot_id = page.insert(payload)
            if slot_id is None:  # pragma: no cover - guarded by the size check
                raise RecordTooLargeError(
                    f"record of {len(payload)} bytes did not fit on a fresh page"
                )

        self._pager.write_page(page)
        self._stats.inserts += 1
        if self._tracer.storage:
            self._tracer.emit(
                RecordInsertedEvent(
                    page_id=page.page_id,
                    slot_id=slot_id,
                    length=len(payload),
                    page_free_space_after=page.free_space,
                )
            )
        return RecordId(page.page_id, slot_id)

    def _extend(self, tail: Page) -> Page:
        """Allocate a new tail page and link it after ``tail``."""
        new_page = self._pager.allocate_page(PageType.HEAP)
        tail.next_page_id = new_page.page_id
        self._pager.write_page(tail)

        self._last_page_id = new_page.page_id
        self._stats.pages_allocated += 1
        if self._on_pages_changed is not None:
            self._on_pages_changed(self._first_page_id, self._last_page_id)
        return new_page

    def delete(self, record_id: RecordId) -> bool:
        """Tombstone a record. Returns ``False`` if it was already gone."""
        page = self._pager.read_page(record_id.page_id)
        if not page.delete(record_id.slot_id):
            return False
        self._pager.write_page(page)
        self._stats.deletes += 1
        if self._tracer.storage:
            self._tracer.emit(
                RecordDeletedEvent(
                    page_id=record_id.page_id, slot_id=record_id.slot_id
                )
            )
        return True

    # -- reads -------------------------------------------------------------

    def get(self, record_id: RecordId) -> bytes:
        """Fetch one record. Raises :class:`RecordNotFoundError` if it is dead."""
        page = self._pager.read_page(record_id.page_id)
        payload = page.read(record_id.slot_id)
        if payload is None:
            raise RecordNotFoundError(f"no live record at {record_id}")
        self._stats.reads += 1
        if self._tracer.verbose:
            self._tracer.emit(
                RecordReadEvent(
                    page_id=record_id.page_id,
                    slot_id=record_id.slot_id,
                    length=len(payload),
                )
            )
        return payload

    def scan(self) -> Iterator[tuple[RecordId, bytes]]:
        """Yield every live record in physical order.

        A generator, so the caller pulls one row at a time and a ``LIMIT`` need
        not materialise the table. That is the same contract the volcano
        executor in Milestone 3 is built on.
        """
        started = time.perf_counter_ns()
        self._stats.scans += 1
        if self._tracer.summary:
            self._tracer.emit(
                HeapScanEvent(action="started", first_page_id=self._first_page_id)
            )

        pages_scanned = 0
        rows_emitted = 0
        page_id = self._first_page_id
        while page_id != INVALID_PAGE_ID:
            page = self._pager.read_page(page_id)
            pages_scanned += 1
            for slot_id, payload in page.iter_records():
                rows_emitted += 1
                yield RecordId(page_id, slot_id), payload
            page_id = page.next_page_id

        if self._tracer.summary:
            self._tracer.emit(
                HeapScanEvent(
                    action="finished",
                    first_page_id=self._first_page_id,
                    pages_scanned=pages_scanned,
                    rows_emitted=rows_emitted,
                    duration_ns=time.perf_counter_ns() - started,
                )
            )

    def page_ids(self) -> Iterator[int]:
        """Walk the page chain, detecting cycles."""
        page_id = self._first_page_id
        seen: set[int] = set()
        while page_id != INVALID_PAGE_ID:
            if page_id in seen:
                raise RecordNotFoundError(f"cycle in heap page chain at page {page_id}")
            seen.add(page_id)
            yield page_id
            page_id = self._pager.read_page(page_id).next_page_id

    def count(self) -> int:
        """Live record count. O(pages) — no cached count exists yet."""
        return sum(
            self._pager.read_page(page_id).live_record_count
            for page_id in self.page_ids()
        )

    def page_count(self) -> int:
        return sum(1 for _ in self.page_ids())

    def __repr__(self) -> str:
        return (
            f"<HeapFile pages={self._first_page_id}..{self._last_page_id} "
            f"inserts={self._stats.inserts}>"
        )
