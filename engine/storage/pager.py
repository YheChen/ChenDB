"""The pager: the only component that touches the database file.

Responsibilities
----------------
1. Map page ids to file offsets — ``offset = page_id * page_size``.  Fixed-size
   pages are what make this arithmetic possible, and that single property is
   why essentially every disk-based database uses them.
2. Own the page allocator: extend the file, or recycle a page from the free list.
3. Verify checksums on read and refresh them on write.
4. Maintain the meta page.

What it deliberately does **not** do
------------------------------------
Caching.  Every :meth:`Pager.read_page` in Milestone 1 is a real ``read``
syscall, and every :meth:`Pager.write_page` is a real ``write``.  Milestone 7
inserts a buffer pool between the heap and the pager; keeping the pager
cache-free now means that change is an insertion, not a rewrite.

Durability model
----------------
``write_page`` hands bytes to the operating system.  It does **not** make them
durable — the OS may hold them in its page cache for seconds.  Only
:meth:`sync` (``fsync``) guarantees the data survives a power loss.

Milestone 1 therefore has a real, honest crash window: a process killed between
a write and a sync can lose recent inserts, and a page torn mid-write is
detected by its checksum but cannot be repaired.  Fixing that is precisely
what the write-ahead log in Milestone 9 is for.

Complexity
----------
=====================  ===========================================
Operation              Cost
=====================  ===========================================
``read_page``          O(1) — one seek + one read of ``page_size``
``write_page``         O(1) — one seek + one write
``allocate_page``      O(1) — free-list pop or file extension
``free_page``          O(1) — free-list push
=====================  ===========================================

Each allocation also rewrites the meta page, so it costs two writes.  A real
system amortises that: PostgreSQL tracks free space in a separate Free Space
Map that is itself buffered and only crash-*hinted*, not crash-safe.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from engine.diagnostics.events import (
    FileSyncEvent,
    PageAllocatedEvent,
    PageFreedEvent,
    PageReadEvent,
    PageWriteEvent,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import CorruptDatabaseError, PageNotFoundError
from engine.storage.constants import (
    DEFAULT_PAGE_SIZE,
    INVALID_PAGE_ID,
    META_PAGE_ID,
    PageType,
)
from engine.storage.meta import MetaPage
from engine.storage.page import Page, validate_page_size

__all__ = ["Pager", "PagerStats"]


@dataclass(slots=True)
class PagerStats:
    """Cumulative I/O counters for one pager instance.

    Cheap to maintain (integer increments) and always on, because they are the
    headline numbers a query plan is judged by.  Milestone 7 will report buffer
    pool hits against ``page_reads`` to show the cache working.
    """

    page_reads: int = 0
    page_writes: int = 0
    allocations: int = 0
    recycled_allocations: int = 0
    frees: int = 0
    syncs: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    read_time_ns: int = 0
    write_time_ns: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "page_reads": self.page_reads,
            "page_writes": self.page_writes,
            "allocations": self.allocations,
            "recycled_allocations": self.recycled_allocations,
            "frees": self.frees,
            "syncs": self.syncs,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "read_time_ns": self.read_time_ns,
            "write_time_ns": self.write_time_ns,
        }


class Pager:
    """Page-granular access to a single database file."""

    __slots__ = (
        "_closed",
        "_file",
        "_meta",
        "_page_size",
        "_path",
        "_stats",
        "_tracer",
        "_verify_checksums",
        "_writes_since_sync",
    )

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        create: bool = True,
        verify_checksums: bool = True,
        tracer: Tracer | None = None,
    ) -> None:
        """Open ``path``, creating and initialising it when ``create`` is set.

        ``page_size`` is only honoured for a brand-new file; an existing file
        keeps whatever size it was created with, and a mismatch is an error
        rather than a silent reinterpretation of the bytes.
        """
        validate_page_size(page_size)
        self._path = Path(path)
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._stats = PagerStats()
        self._verify_checksums = verify_checksums
        self._writes_since_sync = 0
        self._closed = False

        existed = self._path.exists()
        if not existed and not create:
            raise FileNotFoundError(f"no such database file: {self._path}")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # "x+b" would race; open for update, creating an empty file if needed.
        # Held open for the life of the Pager, so this is deliberately not
        # a context manager; Pager.close() owns the lifecycle.
        self._file = open(self._path, "r+b" if existed else "w+b")  # noqa: SIM115

        if existed and self._path.stat().st_size > 0:
            self._meta = self._load_meta()
            self._page_size = self._meta.page_size
            if page_size != DEFAULT_PAGE_SIZE and page_size != self._page_size:
                self._file.close()
                raise CorruptDatabaseError(
                    f"{self._path} was created with {self._page_size}-byte pages; "
                    f"cannot reopen it with {page_size}-byte pages"
                )
            self._check_file_length()
        else:
            self._page_size = page_size
            self._meta = MetaPage(page_size=page_size, page_count=1)
            self._write_meta()
            self.sync()

    # -- properties --------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def page_count(self) -> int:
        """Total pages in the file, including the meta page."""
        return self._meta.page_count

    @property
    def meta(self) -> MetaPage:
        """The live meta page. Mutate it, then call :meth:`flush_meta`."""
        return self._meta

    @property
    def stats(self) -> PagerStats:
        return self._stats

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def closed(self) -> bool:
        return self._closed

    # -- meta page ---------------------------------------------------------

    def _load_meta(self) -> MetaPage:
        self._file.seek(0)
        raw = self._file.read(DEFAULT_PAGE_SIZE)
        return MetaPage.from_bytes(raw, verify_checksum=self._verify_checksums)

    def _write_meta(self) -> None:
        self._write_at(META_PAGE_ID, self._meta.to_bytes())

    def flush_meta(self) -> None:
        """Persist in-memory changes to the meta page."""
        self._write_meta()

    def _check_file_length(self) -> None:
        size = self._path.stat().st_size
        expected = self._meta.page_count * self._page_size
        if size != expected:
            raise CorruptDatabaseError(
                f"{self._path} is {size} bytes but the meta page claims "
                f"{self._meta.page_count} pages of {self._page_size} bytes "
                f"({expected} bytes). The file was truncated or partially written."
            )

    # -- raw I/O -----------------------------------------------------------

    def file_offset(self, page_id: int) -> int:
        """Byte offset of ``page_id``. The whole reason pages are fixed-size."""
        return page_id * self._page_size

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError(f"pager for {self._path} is closed")

    def _check_page_id(self, page_id: int) -> None:
        if not 0 <= page_id < self._meta.page_count:
            raise PageNotFoundError(
                f"page {page_id} is outside the file "
                f"(it holds pages 0..{self._meta.page_count - 1})"
            )

    def _read_at(self, page_id: int) -> bytes:
        offset = self.file_offset(page_id)
        started = time.perf_counter_ns()
        self._file.seek(offset)
        raw = self._file.read(self._page_size)
        elapsed = time.perf_counter_ns() - started

        if len(raw) != self._page_size:
            raise CorruptDatabaseError(
                f"short read on page {page_id}: got {len(raw)} of {self._page_size} bytes"
            )

        self._stats.page_reads += 1
        self._stats.bytes_read += len(raw)
        self._stats.read_time_ns += elapsed
        if self._tracer.storage:
            self._tracer.emit(
                PageReadEvent(
                    page_id=page_id,
                    file_offset=offset,
                    source="disk",
                    duration_ns=elapsed,
                )
            )
        return raw

    def _write_at(self, page_id: int, raw: bytes) -> None:
        if len(raw) != self._page_size:
            raise ValueError(
                f"page {page_id}: refusing to write {len(raw)} bytes "
                f"into a {self._page_size}-byte slot"
            )
        offset = self.file_offset(page_id)
        started = time.perf_counter_ns()
        self._file.seek(offset)
        self._file.write(raw)
        elapsed = time.perf_counter_ns() - started

        self._stats.page_writes += 1
        self._stats.bytes_written += len(raw)
        self._stats.write_time_ns += elapsed
        self._writes_since_sync += 1
        if self._tracer.storage:
            self._tracer.emit(
                PageWriteEvent(
                    page_id=page_id, file_offset=offset, duration_ns=elapsed
                )
            )

    # -- page access -------------------------------------------------------

    def read_raw(self, page_id: int) -> bytes:
        """Return a page's bytes with no interpretation.

        Used by the page inspector, which must be able to show a page whose
        checksum fails or whose type byte is unrecognised.
        """
        self._ensure_open()
        self._check_page_id(page_id)
        return self._read_at(page_id)

    def read_page(self, page_id: int) -> Page:
        """Read and decode a slotted page.

        Page 0 is rejected: the meta page has a different layout and is reached
        through :attr:`meta`.
        """
        self._ensure_open()
        if page_id == META_PAGE_ID:
            raise ValueError(
                "page 0 is the meta page; use Pager.meta or Pager.read_raw(0)"
            )
        self._check_page_id(page_id)
        raw = self._read_at(page_id)
        return Page.from_bytes(
            page_id,
            raw,
            self._page_size,
            verify_checksum=self._verify_checksums,
        )

    def write_page(self, page: Page) -> None:
        """Write a page back, refreshing its checksum first.

        The bytes are handed to the OS, not to the disk. Call :meth:`sync` for
        durability.
        """
        self._ensure_open()
        if page.page_id == META_PAGE_ID:
            raise ValueError("cannot overwrite the meta page with a slotted page")
        self._check_page_id(page.page_id)
        self._write_at(page.page_id, page.to_bytes())

    # -- allocation --------------------------------------------------------

    def allocate_page(self, page_type: PageType) -> Page:
        """Return a fresh, zeroed page of ``page_type``.

        Prefers the free list, so a workload that drops and recreates data
        reuses file space instead of growing the file forever.
        """
        self._ensure_open()
        recycled = self._meta.free_list_head != INVALID_PAGE_ID
        if recycled:
            page_id = self._meta.free_list_head
            # A freed page stores the next free id in its next_page_id field,
            # so the free list needs no space outside the pages themselves.
            freed = Page.from_bytes(
                page_id,
                self._read_at(page_id),
                self._page_size,
                verify_checksum=self._verify_checksums,
            )
            self._meta.free_list_head = freed.next_page_id
        else:
            page_id = self._meta.page_count
            self._meta.page_count += 1

        page = Page.create(page_id, page_type, self._page_size)
        self._write_at(page_id, page.to_bytes())
        self._write_meta()

        self._stats.allocations += 1
        if recycled:
            self._stats.recycled_allocations += 1
        if self._tracer.storage:
            self._tracer.emit(
                PageAllocatedEvent(
                    page_id=page_id, page_type=page_type.name, recycled=recycled
                )
            )
        return page

    def free_page(self, page_id: int) -> None:
        """Return ``page_id`` to the free list.

        The page is zeroed and re-typed as ``FREE``, then pushed onto the head
        of the chain. The file never shrinks — reclaiming trailing pages would
        require proving nothing points at them, which is what ``VACUUM FULL``
        does in PostgreSQL and what ``VACUUM`` does in SQLite.
        """
        self._ensure_open()
        if page_id == META_PAGE_ID:
            raise ValueError("cannot free the meta page")
        self._check_page_id(page_id)

        previous_type = Page.from_bytes(
            page_id,
            self._read_at(page_id),
            self._page_size,
            verify_checksum=self._verify_checksums,
        ).page_type

        page = Page.create(page_id, PageType.FREE, self._page_size)
        page.next_page_id = self._meta.free_list_head
        self._write_at(page_id, page.to_bytes())
        self._meta.free_list_head = page_id
        self._write_meta()

        self._stats.frees += 1
        if self._tracer.storage:
            self._tracer.emit(
                PageFreedEvent(page_id=page_id, previous_type=previous_type.name)
            )

    def free_list(self) -> Iterator[int]:
        """Walk the free-list chain, head first. Diagnostics only."""
        page_id = self._meta.free_list_head
        seen: set[int] = set()
        while page_id != INVALID_PAGE_ID:
            if page_id in seen:
                raise CorruptDatabaseError(f"cycle in free list at page {page_id}")
            seen.add(page_id)
            yield page_id
            page_id = Page.from_bytes(
                page_id,
                self._read_at(page_id),
                self._page_size,
                verify_checksum=self._verify_checksums,
            ).next_page_id

    # -- durability --------------------------------------------------------

    def sync(self) -> None:
        """Flush Python buffers and ``fsync`` the file.

        This is the expensive operation in any database — typically hundreds of
        microseconds on an SSD and milliseconds on spinning rust. Everything
        the WAL does is in service of calling it less often while keeping the
        same guarantee.
        """
        self._ensure_open()
        started = time.perf_counter_ns()
        self._file.flush()
        os.fsync(self._file.fileno())
        elapsed = time.perf_counter_ns() - started

        self._stats.syncs += 1
        if self._tracer.storage:
            self._tracer.emit(
                FileSyncEvent(
                    duration_ns=elapsed,
                    pages_written_since_last_sync=self._writes_since_sync,
                )
            )
        self._writes_since_sync = 0

    def close(self) -> None:
        """Sync and close. Idempotent."""
        if self._closed:
            return
        try:
            self.sync()
        finally:
            self._file.close()
            self._closed = True

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> Pager:
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
        return (
            f"<Pager {self._path.name} {state} "
            f"page_size={self._page_size} pages={self._meta.page_count}>"
        )
