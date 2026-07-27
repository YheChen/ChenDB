"""The pager: the only component that touches the database file.

Responsibilities
----------------
1. Map page ids to file offsets — ``offset = page_id * page_size``.  Fixed-size
   pages are what make this arithmetic possible, and that single property is
   why essentially every disk-based database uses them.
2. Own the page allocator: extend the file, or recycle a page from the free list.
3. Verify checksums on read and refresh them on write.
4. Maintain the meta page.

Caching
-------
Milestones 1-6 had none: every read was a ``pread`` plus a CRC32, and every
write was a ``write``.  Milestone 7 puts a
:class:`~engine.storage.buffer.BufferPool` between this class and the file, and
it was an *insertion* rather than a rewrite — the pager kept the same public
methods, and no caller changed.

The pager remains the only thing that touches the file. The pool reaches storage
solely through the two callbacks this class hands it, which is what keeps that
true and what makes the pool testable against a dictionary.

Logical against physical
------------------------
``PagerStats`` counts both, and the distinction became meaningful in Milestone 7:

* ``page_reads`` / ``page_writes`` — **logical**: how many times the engine
  asked for a page. Unchanged in meaning, so a test asserting "this operation
  reads pages" still measures what it always did.
* ``physical_reads`` / ``physical_writes`` — **syscalls**. The gap between the
  two is the pool working, and ``hit_rate`` is that gap as a fraction.

Durability model
----------------
``write_page`` no longer reaches the operating system at all: it marks a frame
dirty, and the bytes go out on eviction or on :meth:`sync`.  ``sync`` flushes
every dirty frame *before* it ``fsync``s, so the contract a caller sees is
unchanged — after ``sync()`` returns, everything acknowledged is durable.

Between syncs, more data now lives only in memory than before, so **the crash
window is wider**. A process killed after twenty inserts and no sync used to
lose whatever the OS had not flushed; now it loses whatever the pool has not
evicted, which is likely all of it. That is the honest cost of write-back, and
closing it is exactly what Milestone 9's write-ahead log is for.

Complexity
----------
=====================  ===============================================
Operation              Cost
=====================  ===============================================
``read_page``          O(1) — a pool hit, or one seek + one read
``write_page``         O(1) — into a frame; the disk write is deferred
``allocate_page``      O(1) — free-list pop or file extension
``free_page``          O(1) — free-list push
``sync``               O(dirty frames) writes, then one ``fsync``
=====================  ===============================================

Each allocation also rewrites the meta page. That used to cost a second syscall
every time; the meta page is now pooled like any other, so a burst of
allocations dirties one frame and writes it once. PostgreSQL amortises the same
cost with a Free Space Map that is buffered and only crash-*hinted*.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from engine.diagnostics.events import (
    CheckpointEvent,
    FileSyncEvent,
    PageAllocatedEvent,
    PageFreedEvent,
    PageReadEvent,
    PageWriteEvent,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import CorruptDatabaseError, PageNotFoundError
from engine.storage.buffer import DEFAULT_POOL_FRAMES, BufferPool
from engine.storage.constants import (
    DEFAULT_PAGE_SIZE,
    INVALID_PAGE_ID,
    META_PAGE_ID,
    PageType,
)
from engine.storage.meta import MetaPage, read_meta_lsn, stamp_meta_lsn
from engine.storage.page import Page, read_lsn, stamp_lsn, validate_page_size
from engine.wal.log import WAL_SUFFIX, WriteAheadLog
from engine.wal.record import NO_TRANSACTION
from engine.wal.recovery import RecoveryReport, recover

__all__ = ["Pager", "PagerStats"]


@dataclass(slots=True)
class PagerStats:
    """Cumulative I/O counters for one pager instance.

    Cheap to maintain (integer increments) and always on, because they are the
    headline numbers a query plan is judged by.

    ``page_reads`` and ``page_writes`` are **logical** — how many times the
    engine asked. ``physical_reads`` and ``physical_writes`` are syscalls. Before
    Milestone 7 they were the same number; the gap between them now is the
    buffer pool earning its memory.
    """

    page_reads: int = 0
    page_writes: int = 0
    physical_reads: int = 0
    physical_writes: int = 0
    allocations: int = 0
    recycled_allocations: int = 0
    frees: int = 0
    syncs: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    read_time_ns: int = 0
    write_time_ns: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """Logical reads that did not become a syscall."""
        if not self.page_reads:
            return 0.0
        return 1.0 - min(self.physical_reads / self.page_reads, 1.0)

    def as_dict(self) -> dict[str, int | float]:
        return {
            "page_reads": self.page_reads,
            "page_writes": self.page_writes,
            "physical_reads": self.physical_reads,
            "physical_writes": self.physical_writes,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "allocations": self.allocations,
            "recycled_allocations": self.recycled_allocations,
            "frees": self.frees,
            "syncs": self.syncs,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "read_time_ns": self.read_time_ns,
            "write_time_ns": self.write_time_ns,
        }


@dataclass(frozen=True, slots=True)
class WriteIntent:
    """What the transaction layer says about a page that is about to change.

    Returned by the ``on_before_write`` hook, and the only thing the pager knows
    about transactions. Two fields, both of which the log needs and neither of
    which the pager could work out on its own: which transaction to file the
    record under, and — for the *first* write to a page in that transaction —
    the bytes to put it back to.
    """

    transaction_id: int
    before_image: bytes | None = None
    """None when this transaction has already captured this page. Only the
    first write needs an undo image; the rest are redo-only."""


class Pager:
    """Page-granular access to a single database file."""

    __slots__ = (
        "_closed",
        "_file",
        "_meta",
        "_on_before_write",
        "_on_checkpoint",
        "_page_size",
        "_path",
        "_pool",
        "_recovery",
        "_stats",
        "_tracer",
        "_verify_checksums",
        "_wal",
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
        buffer_pool_frames: int = DEFAULT_POOL_FRAMES,
        wal: bool = True,
    ) -> None:
        """Open ``path``, creating and initialising it when ``create`` is set.

        ``page_size`` is only honoured for a brand-new file; an existing file
        keeps whatever size it was created with, and a mismatch is an error
        rather than a silent reinterpretation of the bytes.

        ``wal=False`` opens the file without a log, which means no durability
        guarantee beyond :meth:`sync` — the Milestone 8 behaviour. It exists for
        the benchmark that prices the log and for the page inspector, which
        should be able to look at a damaged file without recovering it first.
        """
        validate_page_size(page_size)
        self._path = Path(path)
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._stats = PagerStats()
        self._verify_checksums = verify_checksums
        self._writes_since_sync = 0
        self._closed = False
        #: Called before any page changes, so a transaction can keep a
        #: before-image. Set by Database; None when nothing is watching.
        self._on_before_write: (
            Callable[[int, Callable[[], bytes], str], WriteIntent | None] | None
        ) = None
        #: Called at the start of a checkpoint, so the owner can bring the meta
        #: page up to date before it is written. Milestone 10 uses it to stamp
        #: ``next_xid``; the pager has no idea what a transaction id is.
        self._on_checkpoint: Callable[[], None] | None = None
        self._wal: WriteAheadLog | None = None
        self._recovery = RecoveryReport()

        # Built before the file is touched, because loading the meta page
        # already goes through it.
        self._pool = BufferPool(
            page_size=page_size,
            capacity=buffer_pool_frames,
            read_through=self._read_from_disk,
            write_through=self._write_to_disk_write_ahead,
            tracer=self._tracer,
        )

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
            # The pool was sized for the requested page size, which the file may
            # disagree with. Rebuild it now the real size is known.
            if self._page_size != page_size:
                self._pool = BufferPool(
                    page_size=self._page_size,
                    capacity=buffer_pool_frames,
                    read_through=self._read_from_disk,
                    write_through=self._write_to_disk_write_ahead,
                    tracer=self._tracer,
                )
            if wal:
                self._open_wal()
                self._recover()
            # After recovery, not before. A crash can leave a file shorter than
            # its meta page claims — the pool evicted the meta page before the
            # pages it references — and with a log that is repairable rather
            # than fatal: redo has an after-image for every page that was ever
            # allocated, because allocating one always wrote it. Checking first
            # would reject a database the very next line could fix.
            self._check_file_length()
        else:
            self._page_size = page_size
            self._meta = MetaPage(page_size=page_size, page_count=1)
            if wal:
                # A brand-new database starts with an empty log at LSN 0. Opening
                # it *before* the first meta write means that write is logged
                # like every other, so there is no page in the file the log has
                # never heard of.
                self._open_wal()
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
    def on_checkpoint(self):
        """Called before a checkpoint writes the meta page. See :meth:`checkpoint`."""
        return self._on_checkpoint

    @on_checkpoint.setter
    def on_checkpoint(self, hook) -> None:
        self._on_checkpoint = hook

    @property
    def on_before_write(self):
        """The hook a transaction manager installs. See :meth:`_write_at`."""
        return self._on_before_write

    @on_before_write.setter
    def on_before_write(self, hook) -> None:
        self._on_before_write = hook

    @property
    def buffer_pool(self) -> BufferPool:
        """The page cache. Read it for the frame grid; do not bypass it."""
        return self._pool

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def closed(self) -> bool:
        return self._closed

    # -- the write-ahead log -----------------------------------------------

    @property
    def wal(self) -> WriteAheadLog | None:
        """The log, or None when this pager was opened without one."""
        return self._wal

    @property
    def recovery(self) -> RecoveryReport:
        """What happened the last time this file was opened.

        Empty on a clean open, which is the point: a caller can tell whether the
        previous process shut down properly by asking, rather than by reading a
        log.
        """
        return self._recovery

    def _open_wal(self) -> None:
        self._wal = WriteAheadLog(
            self._path.with_name(self._path.name + WAL_SUFFIX),
            base_lsn=self._meta.checkpoint_lsn,
            tracer=self._tracer,
        )

    def _recover(self) -> None:
        """Bring the file up to date with the log, if the log has anything.

        Runs before anything else reads a page. The pool is cleared afterwards
        because recovery writes through it and the catalog has not been built
        yet — starting a fresh session on frames populated by a recovery pass
        would work, but leaves the pool's counters describing work the user did
        not do.
        """
        log = self._wal
        if log is None:
            return
        self._recovery = recover(
            log,
            read_page_lsn=self._page_lsn_on_disk,
            apply_page=self._apply_recovered_page,
            tracer=self._tracer,
        )
        if not self._recovery.ran:
            return

        # The pages are current, so replaying the same records again would be
        # wasted work on the next open. Reset the stream past everything just
        # applied and record where it now starts.
        self._pool.flush()
        self._file.flush()
        os.fsync(self._file.fileno())
        base = max(self._recovery.highest_lsn, log.next_lsn)
        self.reload_meta()
        self._write_meta_directly(base)
        log.reset(base)
        self._pool.clear()

    def _page_lsn_on_disk(self, page_id: int) -> int:
        """The LSN a page carries on disk, or ``-1`` if there is no page there.

        ``-1`` and not ``0``, and the difference is a bug that took a test
        against a dictionary to find. **Zero is a real LSN** — it is the first
        record ever written, which for a brand-new database is the meta page. A
        crash before that page reached the disk leaves nothing to read, and
        answering ``0`` would have redo compare ``0 >= 0``, decide the page was
        already current, and skip restoring the only page that makes the file a
        database.

        A page that exists but cannot be read gets the same answer, deliberately:
        a page the crash left torn is exactly the page redo is there to replace,
        and refusing to read it would turn a recoverable database into an
        unopenable one.
        """
        needed = (page_id + 1) * self._page_size
        try:
            if os.fstat(self._file.fileno()).st_size < needed:
                return -1
            self._file.seek(page_id * self._page_size)
            raw = self._file.read(self._page_size)
            if len(raw) < self._page_size:
                return -1
            return read_meta_lsn(raw) if page_id == META_PAGE_ID else read_lsn(raw)
        except OSError:
            return -1

    def _apply_recovered_page(self, page_id: int, image: bytes) -> None:
        """Write a page image straight to the file during recovery.

        Straight to the file, not through the pool: recovery may touch more
        pages than the pool has frames, and admitting each one would evict the
        last for no benefit — nothing is going to read them again in this pass.
        The file is also being *extended* here, past what the meta page in
        memory currently claims, so the pool's bounds checks do not apply yet.
        """
        if len(image) != self._page_size:
            raise CorruptDatabaseError(
                f"log record for page {page_id} holds {len(image)} bytes, "
                f"but this file uses {self._page_size}-byte pages"
            )
        self._extend_file_to(page_id + 1)
        self._file.seek(page_id * self._page_size)
        self._file.write(image)
        self._stats.physical_writes += 1
        self._stats.bytes_written += len(image)

    def checkpoint(self) -> int:
        """Flush every dirty page, then discard the log.

        Returns the number of pages written. This is what bounds the log: every
        record before a checkpoint describes a change the file already has, so
        recovery would skip all of them and keeping them costs disk for nothing.

        The order is the whole correctness argument, and it is not the obvious
        one::

            1. flush dirty pages, fsync the database file
            2. append CHECKPOINT, fsync the log
            3. write the meta page's new checkpoint_lsn straight to the file
            4. truncate the log

        Step 3 is the only place in the engine that writes a page around the log
        and around the pool, and it has to be: logging it would put a record
        into the log this call is about to truncate, and the meta page's new
        ``checkpoint_lsn`` cannot be known until step 2 has decided where the
        log will restart. It is safe precisely because a checkpoint is the
        moment when everything else is already durable.

        Crash between 3 and 4 is the interesting one. The meta page says the log
        restarts at the new LSN, but the file still holds the old records at the
        old one. The next open reads them at the wrong position, the LSN in each
        header fails to match where it was found, and the log correctly reads as
        empty — which it is, in the sense that matters: step 1 already put every
        page those records describe onto the disk.

        Callers should not need this — :meth:`close` does it, and a real system
        runs it on a timer or a log-size threshold. It is public because "run a
        checkpoint and watch the log collapse to nothing" is the clearest
        demonstration of what a checkpoint is for.
        """
        self._ensure_open()
        log = self._wal
        if log is None:
            return 0

        if self._on_checkpoint is not None:
            self._on_checkpoint()
        started = time.perf_counter_ns()
        reclaimed_before = log.stats.bytes_reclaimed

        def flush_pages() -> None:
            self._pool.flush()
            self._file.flush()
            os.fsync(self._file.fileno())

        flushed = self._pool.dirty_pages
        record = log.checkpoint(flush_pages=flush_pages)
        self._write_meta_directly(log.base_lsn)

        if self._tracer.summary:
            self._tracer.emit(
                CheckpointEvent(
                    lsn=record.lsn,
                    pages_flushed=flushed,
                    bytes_reclaimed=log.stats.bytes_reclaimed - reclaimed_before,
                    duration_ns=time.perf_counter_ns() - started,
                )
            )
        return flushed

    def log_commit(self, transaction_id: int) -> int:
        """Make a transaction's commit durable. Returns the record's LSN.

        **This is the one write that makes a commit mean something.** Everything
        else the log does is bookkeeping in service of it: without a commit
        record on disk, nothing distinguishes a transaction that finished from
        one the power cut off half way, however carefully the pages were
        ordered.

        The dirty pages are deliberately *not* flushed — no-force, in ARIES
        terms. They may still be sitting in the buffer pool when this returns,
        and that is safe because the log has enough to reconstruct them.
        Milestone 1's rule that ``sync`` is what makes data durable stops being
        true here: commit is.
        """
        if self._wal is None:
            return 0
        return self._wal.commit(transaction_id).lsn

    def log_abort(self, transaction_id: int) -> int:
        """Record that a transaction was rolled back. Returns the record's LSN.

        Not synced, and it does not need to be. The rollback already wrote every
        before-image back through the ordinary page path, so those restores are
        in the log as update records; a crash before this reaches disk leaves
        recovery replaying them and arriving in the same place. The record
        exists so *analysis* can classify the transaction directly rather than
        having to infer it.
        """
        if self._wal is None:
            return 0
        return self._wal.abort(transaction_id).lsn

    def _write_meta_directly(self, checkpoint_lsn: int) -> None:
        """Persist the meta page without logging it or going through the pool.

        Only :meth:`checkpoint` and :meth:`_recover` call this, and only at the
        two moments when the log is being restarted and so cannot be written to.
        Everything else uses :meth:`_write_meta`, which is logged like any other
        page.
        """
        self._meta.checkpoint_lsn = checkpoint_lsn
        raw = self._meta.to_bytes()
        self._file.seek(0)
        self._file.write(raw)
        self._file.flush()
        os.fsync(self._file.fileno())
        # The pool may still hold the old bytes; drop them rather than write
        # them back over what was just written.
        self._pool.invalidate(META_PAGE_ID)
        self._stats.physical_writes += 1
        self._stats.bytes_written += len(raw)

    # -- meta page ---------------------------------------------------------

    def _load_meta(self) -> MetaPage:
        # Read straight from the file rather than through the pool: the pool is
        # sized in pages and the page size is exactly what this call discovers.
        self._file.seek(0)
        raw = self._file.read(DEFAULT_PAGE_SIZE)
        return MetaPage.from_bytes(raw, verify_checksum=self._verify_checksums)

    def _write_meta(self) -> None:
        self._write_at(META_PAGE_ID, self._meta.to_bytes(), reason="meta page")

    def reload_meta(self) -> None:
        """Re-read the meta page from the pool into memory.

        Needed after a rollback: the meta page is a *decoded dataclass* held in
        this object, so restoring its bytes underneath does not change
        ``page_count``, ``free_list_head`` or ``next_object_id``. Everything
        else in the engine reads pages, so everything else recovers for free —
        this is the one piece of engine state that is not on a page it reads.
        """
        raw = self._pool.fetch(META_PAGE_ID)
        self._meta = MetaPage.from_bytes(bytes(raw), verify_checksum=self._verify_checksums)

    def flush_meta(self) -> None:
        """Persist in-memory changes to the meta page."""
        self._write_meta()

    def _check_file_length(self) -> None:
        """Refuse a file *shorter* than the meta page claims. Longer is fine.

        The check is one-sided, and Milestone 8 is why. A rolled-back
        transaction restores the meta page's ``page_count``, but the file was
        already extended and cannot be un-extended safely — so a rolled-back
        allocation leaves trailing pages nothing references. That is also
        exactly the state a crash between extending the file and updating the
        meta page leaves, and it is harmless: the pages are unreachable and the
        next allocation reuses their ids.

        Short is the dangerous direction and stays an error: it means a page the
        meta page believes in is simply not there.
        """
        size = self._path.stat().st_size
        expected = self._meta.page_count * self._page_size
        if size < expected:
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

    # The two callbacks the buffer pool is given. Nothing else in the engine
    # calls them: every other path goes through the pool, which is what makes
    # "the pager is the only thing that touches the file" still true.

    def _read_from_disk(self, page_id: int) -> bytes:
        """One real ``pread``. Only the pool calls this."""
        offset = self.file_offset(page_id)
        started = time.perf_counter_ns()
        self._file.seek(offset)
        raw = self._file.read(self._page_size)
        elapsed = time.perf_counter_ns() - started

        if len(raw) != self._page_size:
            raise CorruptDatabaseError(
                f"short read on page {page_id}: got {len(raw)} of {self._page_size} bytes"
            )

        self._stats.physical_reads += 1
        self._stats.bytes_read += len(raw)
        self._stats.read_time_ns += elapsed
        return raw

    def _write_to_disk(self, page_id: int, raw: bytes) -> None:
        """One real ``write``, on eviction or flush. Only the pool calls this."""
        offset = self.file_offset(page_id)
        started = time.perf_counter_ns()
        self._file.seek(offset)
        self._file.write(raw)
        elapsed = time.perf_counter_ns() - started

        self._stats.physical_writes += 1
        self._stats.bytes_written += len(raw)
        self._stats.write_time_ns += elapsed
        self._writes_since_sync += 1

    # -- logical page access, through the pool -----------------------------

    def _fetch(self, page_id: int) -> tuple[bytes, bool]:
        """Fetch a page's bytes; the flag says whether the pool served them.

        Callers need the flag because a page that came out of a frame has
        **already been verified** — see :meth:`read_page`.

        Emits ``PageReadEvent`` for *every* logical read, not only the ones that
        reach the disk — the ``source`` field is what distinguishes them, and it
        has been in the schema since Milestone 1 waiting for this. Emitting only
        on a miss would also break step mode's "run until the next page read",
        which would start skipping cached reads.
        """
        started = time.perf_counter_ns()
        misses_before = self._pool.stats.misses
        raw = self._pool.fetch(page_id)
        elapsed = time.perf_counter_ns() - started
        cached = self._pool.stats.misses == misses_before

        self._stats.page_reads += 1
        if self._tracer.storage:
            self._tracer.emit(
                PageReadEvent(
                    page_id=page_id,
                    file_offset=self.file_offset(page_id),
                    source="buffer_pool" if cached else "disk",
                    duration_ns=elapsed,
                )
            )
        return raw, cached

    def _read_at(self, page_id: int) -> bytes:
        """Bytes only, for callers that do not care where they came from."""
        return self._fetch(page_id)[0]

    def _write_at(
        self, page_id: int, raw: bytes, reason: str = "", *, capture: bool = True
    ) -> None:
        """Hand a page to the pool. The disk write happens later, if at all.

        **Every page change in the engine passes through here** — heap rows,
        B+ tree nodes, both catalog tables, the meta page — which is what makes
        this the one place a transaction needs to hook. Milestone 8 installs
        ``on_before_write`` and gets undo for the whole engine at once, with no
        subsystem knowing transactions exist.

        ``capture=False`` is for the two callers that must save the before-image
        themselves, earlier: :meth:`allocate_page` and :meth:`free_page` drop the
        page from the pool before writing it, so by the time this runs the old
        bytes are gone. A brand-new page passes ``capture=False`` because it has
        no "before" at all — undoing its allocation is what restoring the meta
        page does.
        """
        if len(raw) != self._page_size:
            raise ValueError(
                f"page {page_id}: refusing to write {len(raw)} bytes "
                f"into a {self._page_size}-byte slot"
            )
        intent: WriteIntent | None = None
        if capture and self._on_before_write is not None:
            intent = self._on_before_write(page_id, lambda: self._pool.fetch(page_id), reason)
        if self._wal is not None:
            raw = self._log_change(page_id, raw, intent)
        started = time.perf_counter_ns()
        physical_before = self._stats.physical_writes
        self._pool.store(page_id, raw)
        elapsed = time.perf_counter_ns() - started

        self._stats.page_writes += 1
        if self._tracer.storage:
            self._tracer.emit(
                PageWriteEvent(
                    page_id=page_id,
                    file_offset=self.file_offset(page_id),
                    duration_ns=elapsed,
                    deferred=self._stats.physical_writes == physical_before,
                )
            )

    def _log_change(
        self, page_id: int, raw: bytes, intent: WriteIntent | None
    ) -> bytes:
        """Log this change and stamp the record's LSN into the page.

        The order matters and is not the obvious one. The page has to *carry*
        the LSN of the record that describes it, and the record has to *contain*
        the page — so neither can be built first. The way out is that an LSN is
        a byte offset, which the log can predict before anything is written:

            lsn = log.next_lsn        what the record will be
            raw = stamp(raw, lsn)     the page now knows its record
            log.append(after=raw)     the record now contains the page

        Get this wrong and the after-image in the log carries an LSN of 0, so
        redo writes a page that still looks un-redone, and every subsequent
        recovery redoes it again.
        """
        log = self._wal
        assert log is not None
        transaction_id = intent.transaction_id if intent is not None else NO_TRANSACTION
        before = intent.before_image if intent is not None else None

        stamped: bytes = b""

        def image_at(lsn: int) -> bytes:
            nonlocal stamped
            if page_id == META_PAGE_ID:
                # Page 0 has its own header: LSN at offset 60, checksum at the
                # end. Using the slotted-page stamper here writes a u64 over the
                # format version and a u32 over the magic.
                stamped = stamp_meta_lsn(raw, lsn)
                # …and the meta page is a decoded dataclass held in this object,
                # so the stamp would be lost the next time it is re-encoded.
                self._meta.lsn = lsn
            else:
                stamped = stamp_lsn(raw, lsn)
            return stamped

        log.append_update(
            transaction_id=transaction_id,
            page_id=page_id,
            before_image=before or b"",
            after_image_for=image_at,
        )
        return stamped

    def _write_to_disk_write_ahead(self, page_id: int, raw: bytes) -> None:
        """The pool's write-through, with the write-ahead rule in front of it.

        **This is the rule the whole milestone rests on.** A page may not reach
        the database file before the log record describing it is at least in the
        OS's hands. Violate it and a crash can leave a change on the pages that
        the log has no record of — which recovery cannot undo, because it cannot
        see it.

        The pool steals: it evicts dirty pages belonging to transactions that
        have not committed. That is allowed *because* of this line, and it is
        why the pool did not have to change to become crash-safe.
        """
        log = self._wal
        if log is not None:
            lsn = read_meta_lsn(raw) if page_id == META_PAGE_ID else read_lsn(raw)
            log.flush_through(lsn)
        self._write_to_disk(page_id, raw)

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

        **A page served from the pool is not re-verified.** Its checksum was
        checked when it was admitted, or its bytes came from ``Page.to_bytes``,
        which recomputes the checksum — so a frame's contents are valid by
        construction and re-checking them proves nothing.

        That is not a micro-optimisation. Verification is a CRC32 over the whole
        page *plus* ``validate()``, which walks every slot; on a 4 KiB page
        holding a hundred rows that is a hundred ``struct`` unpacks. Paying it
        per logical read made the buffer pool almost worthless — it removed the
        syscall and left the expensive half in place. Real systems verify at
        admission for exactly this reason.

        The cost is that in-memory corruption of a frame goes undetected. That
        is the same bet PostgreSQL makes: ``data_checksums`` protects the
        *storage* path, not RAM, and ECC memory is the answer to the other one.
        """
        self._ensure_open()
        if page_id == META_PAGE_ID:
            raise ValueError("page 0 is the meta page; use Pager.meta or Pager.read_raw(0)")
        self._check_page_id(page_id)
        raw, cached = self._fetch(page_id)
        return Page.from_bytes(
            page_id,
            raw,
            self._page_size,
            verify_checksum=self._verify_checksums and not cached,
            validate=not cached,
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
        self._write_at(page.page_id, page.to_bytes(), reason=page.page_type.name.lower())

    def _extend_file_to(self, page_count: int) -> None:
        """Make the file at least ``page_count`` pages long."""
        needed = page_count * self._page_size
        self._file.flush()
        if os.fstat(self._file.fileno()).st_size < needed:
            os.ftruncate(self._file.fileno(), needed)

    def restore_page(self, page_id: int, image: bytes) -> None:
        """Write a before-image back during a rollback, without capturing one.

        Capture is deliberately off: the hook exists to save the *previous*
        contents of a page about to change, and a rollback is not a change to be
        undone — it is the undoing. Capturing here would have the transaction
        save the very page it is restoring.
        """
        self._ensure_open()
        self._write_at(page_id, image, reason="rollback", capture=False)

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
            # Milestone 8 pre-extended the file here with an ftruncate, because
            # the pool is free to evict the meta page before the page it
            # references and _check_file_length refuses a file shorter than its
            # own page count. The log makes that unnecessary: every allocated
            # page is written, every write is logged, so recovery has an image
            # for anything the crash left missing and extends the file putting
            # it back. The length check now runs *after* recovery, for exactly
            # this reason.

        page = Page.create(page_id, page_type, self._page_size)
        # A recycled page has a meaningful before-image — it is on the free
        # list, and rolling back has to put it back there — so capture it
        # *before* invalidate() discards it. A brand-new page has no before at
        # all: it is past the old page_count and not yet in the file, so undoing
        # its allocation is entirely a matter of restoring the meta page.
        if recycled and self._on_before_write is not None:
            self._on_before_write(
                page_id,
                lambda: self._pool.fetch(page_id),
                "recycled from the free list",
            )
        # Whatever was cached under this id is superseded wholesale. Dropping it
        # avoids writing bytes that are already dead — a recycled page would
        # otherwise be flushed once with its old contents.
        self._pool.invalidate(page_id)
        self._write_at(page_id, page.to_bytes(), reason="allocated", capture=False)
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
        # Capture before invalidate(), which drops the frame without writing
        # it back: a page dirtied earlier in this transaction has a stale copy
        # on disk, so re-reading it afterwards would save the wrong bytes.
        if self._on_before_write is not None:
            self._on_before_write(page_id, lambda: self._pool.fetch(page_id), "freed")
        self._pool.invalidate(page_id)
        self._write_at(page_id, page.to_bytes(), reason="freed", capture=False)
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
        # The log first, and not only because of the write-ahead rule: a caller
        # who asked for durability wants the *commit records* durable too, and
        # those live nowhere else.
        if self._wal is not None:
            self._wal.flush(sync=True)
        # Dirty frames next: without this the fsync would make durable only
        # whatever happened to have been evicted, and the contract callers have
        # relied on since Milestone 1 would quietly stop holding.
        self._pool.flush()
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

    def abandon(self) -> None:
        """Drop everything unwritten and let go of the file. **Loses data.**

        What a crash leaves, reproduced on purpose: dirty frames are discarded
        rather than flushed, no checkpoint runs, and the log keeps whatever it
        has already pushed to the OS. Reopening the file then goes through
        recovery exactly as it would after a power cut.

        The staged log buffer is dropped too, which is the honest part — records
        that never reached the OS died with the process in a real crash, and
        pretending otherwise would make the demonstration easier than reality.

        Only the ``POST /crash`` endpoint calls this. It exists so the visualizer
        can show recovery happening rather than describe it, and there is no way
        to write that demonstration honestly with a cooperative shutdown path.
        """
        if self._closed:
            return
        try:
            if self._wal is not None:
                self._wal.abandon()
        finally:
            self._pool.clear(flush=False)
            self._file.close()
            self._closed = True

    def close(self) -> None:
        """Checkpoint and close. Idempotent.

        A clean shutdown ends with a checkpoint, which leaves an empty log — so
        the next open finds nothing to recover and says so. That is what makes
        ``RecoveryReport.ran`` mean "the last process did not shut down
        cleanly" rather than "there were records lying around".
        """
        if self._closed:
            return
        try:
            if self._wal is not None:
                self.checkpoint()
            else:
                self.sync()
        finally:
            if self._wal is not None:
                self._wal.close()
            self._pool.clear()
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
            f"page_size={self._page_size} pages={self._meta.page_count} "
            f"pool={self._pool.resident_pages}/{self._pool.capacity}>"
        )
