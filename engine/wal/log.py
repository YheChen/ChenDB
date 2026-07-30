"""The log file: append, flush, scan, truncate.

    shop.chendb        the pages
    shop.chendb-wal    what happened to them, in order

Two rules make this a *write-ahead* log rather than a diary. Both are enforced
elsewhere and stated here because this module is where they are paid for:

1. **A page may not reach the database file before the record describing it is
   durable.** :meth:`flush_through` is what the buffer pool calls to satisfy
   this, on the eviction path. Break it and recovery finds a page whose change
   it has no record of, and cannot undo.
2. **A commit is not a commit until its record is durable.** :meth:`commit`
   appends and ``fsync``s. That one write is what makes a finished transaction
   distinguishable from an interrupted one after the power comes back, which
   is precisely what Milestone 8 could not do.

Everything else the WAL buys follows from rule 2. Because a commit is durable
without the *pages* being durable, the pool no longer has to flush anything at
commit time, the **no-force** policy, in ARIES vocabulary. And because rule 1
makes an evicted uncommitted page recoverable, the pool may keep stealing. Steal
and no-force together are the fastest pair, and they are the pair that needs a
log; ChenDB has had steal since Milestone 7 and gets no-force here.

Group commit
------------
``fsync`` is the expensive call in a database, hundreds of microseconds on an
SSD. One per commit puts a hard ceiling on commit throughput that has nothing to
do with how much work each transaction did. Real systems amortise it by letting
concurrent committers share a flush. ChenDB has one writer, so there is nobody
to share with and :attr:`syncs` counts one per commit; :meth:`set_sync_policy`
exists so the benchmark can measure what the fsync actually costs by turning it
off, and so the visualizer can show the difference. Turning it off is not a
durability option, it is a measurement.

LSN and the base
----------------
An LSN is a byte position in the *stream*, and the stream outlives the file. A
checkpoint truncates the file, so byte 0 of the file stops being byte 0 of the
stream; :attr:`base_lsn` is the difference, and it is persisted in the meta page
so LSNs stay globally monotonic across truncations. Without that, page LSNs
written before a checkpoint would compare greater than records written after it,
and redo would skip work it needed to do.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final

from engine.diagnostics.events import WalAppendEvent, WalFlushEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.wal.record import (
    NO_TRANSACTION,
    LogRecord,
    RecordType,
    decode_record,
)

__all__ = ["WAL_SUFFIX", "WalStats", "WriteAheadLog"]

#: Appended to the database filename. SQLite uses ``-wal`` for the same purpose
#: and the same reason: adjacent in a directory listing, and obviously derived.
WAL_SUFFIX: Final = "-wal"


@dataclass(slots=True)
class WalStats:
    """Counters for the log, cumulative for the open handle."""

    records_appended: int = 0
    bytes_appended: int = 0
    flushes: int = 0
    """Times the buffer was pushed to the OS."""
    syncs: int = 0
    """Times it was pushed all the way to the platter. The expensive one."""
    sync_ns: int = 0
    """Total time in ``fsync``. Divided by :attr:`syncs`, this is the number
    that decides how fast commits can possibly go."""
    records_coalesced: int = 0
    """Appends that replaced a staged record for the same page instead of
    following it. Every one is a page image not written."""
    checkpoints: int = 0
    bytes_reclaimed: int = 0
    """Log bytes discarded by checkpoints."""

    @property
    def mean_sync_ns(self) -> float:
        return self.sync_ns / self.syncs if self.syncs else 0.0


class WriteAheadLog:
    """An append-only record stream beside the database file."""

    __slots__ = (
        "_base_lsn",
        "_buffer",
        "_buffer_bytes",
        "_buffer_tail",
        "_closed",
        "_file",
        "_flushed_lsn",
        "_last_lsn_of",
        "_path",
        "_stats",
        "_sync_on_commit",
        "_tracer",
    )

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        base_lsn: int = 0,
        tracer: Tracer | None = None,
        sync_on_commit: bool = True,
    ) -> None:
        self._path = Path(path)
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._stats = WalStats()
        self._sync_on_commit = sync_on_commit
        self._closed = False
        self._base_lsn = base_lsn
        #: Records staged in memory. Appending is a list append; the syscall
        #: happens at flush. Milestone 7 made the same trade for pages.
        self._buffer: list[bytes] = []
        #: Running total of :attr:`_buffer`, maintained rather than recomputed.
        #: ``next_lsn`` is read once per append and summing the buffer there
        #: made a transaction of *n* records cost O(n squared), 2,000 rows
        #: updated in one statement spent 8.5 of 9.8 seconds inside ``sum``.
        self._buffer_bytes = 0
        #: The last staged record, kept decoded so the next append can see
        #: whether it supersedes it. See :meth:`append_update`.
        self._buffer_tail: LogRecord | None = None
        #: Per-transaction backward chain, for ``prev_lsn``.
        self._last_lsn_of: dict[int, int] = {}

        self._path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._path.exists()
        self._file = open(self._path, "r+b" if existed else "w+b")  # noqa: SIM115
        self._file.seek(0, os.SEEK_END)
        self._flushed_lsn = self._base_lsn + self._file.tell()

    # -- position ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def base_lsn(self) -> int:
        """The LSN of the log file's first byte."""
        return self._base_lsn

    @property
    def flushed_lsn(self) -> int:
        """Everything below this LSN has reached the OS."""
        return self._flushed_lsn

    @property
    def next_lsn(self) -> int:
        """The LSN the next appended record will be given.

        Readable *before* appending, which the pager depends on: a page carries
        the LSN of the record that describes it, so the LSN has to exist before
        the page image is encoded into that record.
        """
        return self._flushed_lsn + self._buffer_bytes

    @property
    def buffered_bytes(self) -> int:
        return self._buffer_bytes

    @property
    def stats(self) -> WalStats:
        return self._stats

    @property
    def closed(self) -> bool:
        return self._closed

    def set_sync_policy(self, *, sync_on_commit: bool) -> None:
        """Turn the per-commit ``fsync`` off, for measurement only.

        With this off a commit is durable against a *process* crash and not
        against a *machine* crash, which is the distinction between SQLite's
        ``synchronous=NORMAL`` and ``FULL``. The benchmark uses it to price the
        fsync; nothing in the engine turns it off on its own.
        """
        self._sync_on_commit = sync_on_commit

    # -- appending ---------------------------------------------------------

    def append(
        self,
        record_type: RecordType,
        *,
        transaction_id: int = NO_TRANSACTION,
        page_id: int = 0,
        before_image: bytes = b"",
        after_image: bytes = b"",
    ) -> LogRecord:
        """Stage one record. Returns it, with its LSN filled in."""
        self._ensure_open()
        record = LogRecord(
            lsn=self.next_lsn,
            prev_lsn=self._last_lsn_of.get(transaction_id, 0),
            transaction_id=transaction_id,
            record_type=record_type,
            page_id=page_id,
            before_image=before_image,
            after_image=after_image,
        )
        encoded = record.to_bytes()
        self._buffer.append(encoded)
        self._buffer_bytes += len(encoded)
        self._buffer_tail = record
        if transaction_id != NO_TRANSACTION:
            self._last_lsn_of[transaction_id] = record.lsn

        self._stats.records_appended += 1
        self._stats.bytes_appended += record.size
        if self._tracer.storage:
            self._tracer.emit(
                WalAppendEvent(
                    lsn=record.lsn,
                    transaction_id=transaction_id,
                    record_type=record_type.name.lower(),
                    page_id=page_id,
                    prev_lsn=record.prev_lsn,
                    size=record.size,
                )
            )
        return record

    def append_update(
        self,
        *,
        transaction_id: int,
        page_id: int,
        before_image: bytes,
        after_image_for: Callable[[int], bytes],
    ) -> LogRecord:
        """Log a page change, coalescing with the record it supersedes.

        Writing the same page twice in a row (which is what a bulk insert does,
        row after row into the same heap page) produces two records of which
        only the second matters: redo replays them in order and the first is
        immediately overwritten. So if the previous staged record is an update
        to this same page by this same transaction, this one **replaces** it
        rather than following it.

        That is safe only while the record is still *staged*. Once it has been
        flushed, a page carrying its LSN may already be on the disk, and
        rewriting history behind that page would leave the two disagreeing. The
        write-ahead rule guarantees the two cannot overlap: a page reaching the
        disk forces a flush first, which empties the buffer, so nothing
        flushed is ever a coalescing candidate.

        ``after_image_for`` is a callback rather than bytes because the image
        has to be stamped with the record's LSN, and which LSN that is depends
        on whether this call coalesces. The caller cannot know before asking.

        What this does **not** fix is the amplification across flushes. A
        transaction big enough to fill the log buffer still writes a page image
        per flush boundary, and a transaction spread over many statements writes
        one per statement. Fixing that properly means logging *deltas* rather
        than pages, which is the trade ``record.py`` describes.
        """
        self._ensure_open()
        tail = self._buffer_tail
        supersedes = (
            tail is not None
            and tail.record_type is RecordType.UPDATE
            and tail.page_id == page_id
            and tail.transaction_id == transaction_id
        )
        if not supersedes:
            after = after_image_for(self.next_lsn)
            return self.append(
                RecordType.UPDATE,
                transaction_id=transaction_id,
                page_id=page_id,
                before_image=before_image,
                after_image=after,
            )

        assert tail is not None
        # Keep the superseded record's LSN and its before-image: the before is
        # the state at the *transaction's* first touch, which this write does
        # not change, and reusing the LSN is what keeps the stream contiguous
        # without having to move every record after it.
        after = after_image_for(tail.lsn)
        record = LogRecord(
            lsn=tail.lsn,
            prev_lsn=tail.prev_lsn,
            transaction_id=transaction_id,
            record_type=RecordType.UPDATE,
            page_id=page_id,
            before_image=tail.before_image,
            after_image=after,
        )
        encoded = record.to_bytes()
        self._buffer_bytes += len(encoded) - len(self._buffer[-1])
        self._buffer[-1] = encoded
        self._buffer_tail = record
        self._stats.records_coalesced += 1
        return record

    def commit(self, transaction_id: int) -> LogRecord:
        """Append a commit record and make it durable.

        The ``fsync`` here is the whole point of the milestone, and it is the
        only place in the engine that has to happen at commit time. The dirty
        *pages* are left where they are.
        """
        record = self.append(RecordType.COMMIT, transaction_id=transaction_id)
        self.flush(sync=self._sync_on_commit)
        self._last_lsn_of.pop(transaction_id, None)
        return record

    def abort(self, transaction_id: int) -> LogRecord:
        """Append an abort record.

        Not synced. A rollback already put the pages back through the ordinary
        write path, so those restores are in the log as ``UPDATE`` records; if
        the machine dies before this reaches disk, recovery replays them and
        reaches the same place. The record is written so that *analysis* can
        say "this one finished" rather than having to prove it from the updates.
        """
        record = self.append(RecordType.ABORT, transaction_id=transaction_id)
        self._last_lsn_of.pop(transaction_id, None)
        return record

    # -- durability --------------------------------------------------------

    def flush(self, *, sync: bool = False) -> int:
        """Push staged records to the OS, and optionally to the disk.

        Returns the new :attr:`flushed_lsn`.
        """
        self._ensure_open()
        if self._buffer:
            payload = b"".join(self._buffer)
            self._buffer.clear()
            self._buffer_bytes = 0
            self._buffer_tail = None
            self._file.write(payload)
            self._file.flush()
            self._flushed_lsn += len(payload)
            self._stats.flushes += 1

        if sync:
            started = time.perf_counter_ns()
            os.fsync(self._file.fileno())
            elapsed = time.perf_counter_ns() - started
            self._stats.syncs += 1
            self._stats.sync_ns += elapsed
            if self._tracer.storage:
                self._tracer.emit(
                    WalFlushEvent(
                        up_to_lsn=self._flushed_lsn,
                        bytes_written=self._stats.bytes_appended,
                        duration_ns=elapsed,
                        synced=True,
                    )
                )
        return self._flushed_lsn

    def flush_through(self, lsn: int) -> None:
        """Ensure everything up to ``lsn`` has reached the OS.

        **This is the write-ahead rule.** The buffer pool calls it before
        writing a dirty page to the database file, passing that page's LSN. A
        page whose record is still in this process's memory must not be allowed
        onto the disk ahead of it: a crash in between leaves a change on the
        pages that the log has no record of, and recovery cannot undo what it
        cannot see.

        No ``fsync``. Reaching the OS is enough to survive a *process* crash,
        which is what the eviction path is racing. Surviving a *machine* crash
        is what commit's fsync is for, and paying for one per eviction would
        make the pool cost more than the disk it is avoiding.
        """
        if lsn > self._flushed_lsn:
            self.flush()

    # -- reading -----------------------------------------------------------

    def read_all(self) -> tuple[list[LogRecord], bool]:
        """Every whole record in the file, and whether the tail was truncated.

        Reads the file rather than the staged buffer, because the only caller is
        recovery, which runs before anything has been staged.
        """
        self._ensure_open()
        self._file.seek(0)
        raw = self._file.read()
        records: list[LogRecord] = []
        offset = 0
        while offset < len(raw):
            record = decode_record(raw, offset, self._base_lsn)
            if record is None:
                return records, True
            records.append(record)
            offset += record.size
        return records, False

    def records(self) -> Iterator[LogRecord]:
        """Whole records only, for the diagnostics view."""
        yield from self.read_all()[0]

    # -- checkpoint --------------------------------------------------------

    def checkpoint(self, *, flush_pages) -> LogRecord:
        """Discard the log, once the pages it describes are safely on disk.

        The order is the entire correctness argument, and it is the reverse of
        the write-ahead rule:

        1. ``flush_pages()``. Every dirty page reaches the database file and is
           ``fsync``ed. Provided by the pager, because the log has no idea what
           a page is.
        2. Append ``CHECKPOINT`` and ``fsync`` the log. Now the disk says the
           pages are current.
        3. Truncate. Nothing before this point can be needed again: redo has
           nothing to catch up and no transaction is in flight.

        Doing 3 before 2 would lose the record on a crash between them and leave
        recovery replaying a log it no longer has. Doing 1 after 2 would claim
        pages were safe before they were.

        This is a **sharp** checkpoint: it stops the world and flushes
        everything. Real systems use *fuzzy* checkpoints, which record the set of
        dirty pages and the active transactions and let both keep moving, because
        stopping a hundred-gigabyte buffer pool to flush it is not an option. The
        sharp version is correct, much easier to argue about, and the reason
        recovery here never has to reconstruct a dirty-page table.
        """
        self._ensure_open()
        flush_pages()
        record = self.append(RecordType.CHECKPOINT)
        self.flush(sync=True)

        reclaimed = self._flushed_lsn - self._base_lsn
        self._file.truncate(0)
        self._file.seek(0)
        self._buffer_tail = None
        self._base_lsn = self._flushed_lsn
        self._last_lsn_of.clear()

        self._stats.checkpoints += 1
        self._stats.bytes_reclaimed += reclaimed
        return record

    def reset(self, base_lsn: int) -> None:
        """Empty the log and restart the stream at ``base_lsn``.

        Recovery calls this once it has finished: the records have been applied
        and the database file is current, so keeping them would mean replaying
        them again on the next open.
        """
        self._ensure_open()
        self._buffer.clear()
        self._buffer_bytes = 0
        self._buffer_tail = None
        self._file.truncate(0)
        self._file.seek(0)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._base_lsn = base_lsn
        self._flushed_lsn = base_lsn
        self._last_lsn_of.clear()

    # -- lifecycle ---------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("the write-ahead log is closed")

    def abandon(self) -> None:
        """Close without flushing. Staged records are lost, as in a crash."""
        if self._closed:
            return
        self._buffer.clear()
        self._buffer_bytes = 0
        self._buffer_tail = None
        self._file.close()
        self._closed = True

    def close(self) -> None:
        """Flush, sync and close. Idempotent."""
        if self._closed:
            return
        try:
            self.flush(sync=True)
        finally:
            self._file.close()
            self._closed = True

    def __enter__(self) -> WriteAheadLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<WriteAheadLog {self._path.name} base={self._base_lsn} "
            f"next={self.next_lsn} records={self._stats.records_appended}>"
        )
