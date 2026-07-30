"""The buffer pool: making a page read stop being a syscall.

Milestones 1-6 gave the pager no memory at all.  Every ``read_page`` was a
``pread`` plus a CRC32 over 4 KiB, and every ``write_page`` was a ``write``, so
an index build that touched one leaf two hundred times wrote it two hundred
times.  This is the layer that fixes both.

    HeapFile / BPlusTree
            │  read_page(7)
            ▼
    ┌───────────────────────────────────────────┐
    │  BufferPool                               │
    │    page 7 resident?  ── yes ──▶ hit       │   no syscall, no checksum
    │           │ no                            │
    │           ▼                               │
    │    a free frame?  ── no ──▶ evict LRU     │   write it back if dirty
    │           │                               │
    │           ▼  read from disk, admit        │
    └───────────────────────────────────────────┘
            │
            ▼
        the file

Two wins, and the second is the larger one. Reads skip the syscall *and* the
checksum. Writes are **deferred**: ``store`` marks a frame dirty and the bytes
reach the disk on eviction or on ``flush``, so a page written repeatedly is
written to disk once.

Why there are no pin counts
---------------------------
A textbook buffer pool hands out a *pointer into the frame* and requires callers
to **pin** it, for two reasons: two callers must see each other's writes, and a
frame must not be reused while someone still holds it.  Getting the second wrong
lets eviction hand a frame to a new page while another caller is still writing
into it, which corrupts data silently.

ChenDB copies **out of** the frame on read and **into** it on write.  A caller's
``Page`` is therefore an independent object, and eviction can never invalidate
one: if a page is evicted while a caller holds a copy, the caller's later
``write_page`` simply re-admits it with the new bytes.  Correct by construction,
and no caller in the engine had to change.

That is affordable because a 4 KiB ``memcpy`` is roughly a tenth of the syscall
it replaces (measured; see ``docs/performance.md``). It is not free, and it is
not equivalent:

* two callers mutating the same page still lose one of the updates, exactly as
  they did before the pool existed. The database-level write lock is what
  prevents that, not the pool.
* a shared-frame pool would need pinning, and becomes the right design the
  moment there are concurrent readers, which is Milestone 10.

Pin counts that are always zero would be *ceremony*: a number in the UI that
never prevents anything. Saying why they are absent is more useful than
displaying a fake one.

Eviction: real LRU, and why nobody ships it
-------------------------------------------
Residency is an ``OrderedDict``, so "least recently used" is
``popitem(last=False)`` and touching a page is ``move_to_end``, both O(1), with
no scan.

Real systems do not do this. True LRU performs a *write* to shared state on
every read, which under concurrency means taking a lock on the hottest path in
the engine. PostgreSQL uses a **clock sweep** instead: each frame has a small
usage counter, a read only increments it, and the evictor walks a circular
buffer decrementing counters until it finds a zero. Approximate LRU, no lock on
read. That is a concurrency optimisation, and ChenDB has one writer, so exact
LRU costs nothing here and is easier to reason about.

The other reason to avoid plain LRU is **sequential flooding**: a scan of a
table larger than the pool touches every page once and evicts everything useful,
leaving the pool full of pages nobody will read again. PostgreSQL confines large
scans to a small ring buffer for exactly this. ChenDB does not, and
``examples/milestone7_buffer_pool.py`` shows it happening.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Final

from engine.diagnostics.events import BufferPoolEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer

__all__ = [
    "DEFAULT_POOL_FRAMES",
    "MIN_POOL_FRAMES",
    "BufferPool",
    "BufferPoolStats",
    "FrameSnapshot",
    "PoolSnapshot",
]

#: Frames in a pool by default. 128 x 4 KiB = 512 KiB, which is enough to hold a
#: small database entirely and small enough that a real workload evicts.
#: PostgreSQL's ``shared_buffers`` defaults to 128 MB for the same kind of
#: reason, big enough to matter, small enough not to assume the machine.
DEFAULT_POOL_FRAMES: Final = 128

#: Below this, eviction happens so often the pool costs more than it saves. Two
#: frames is also the minimum for the engine to hold a page while reading
#: another, which the heap does on every extend.
MIN_POOL_FRAMES: Final = 2


@dataclass(slots=True)
class BufferPoolStats:
    """What the pool did. The hit rate is the number worth watching."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    dirty_evictions: int = 0
    """Evictions that had to write the frame back before reusing it."""
    writes_absorbed: int = 0
    """Logical writes that did not reach the disk, because the page was already
    dirty in a frame. This is the write-back win, counted directly."""
    flushes: int = 0
    pages_flushed: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "lookups": self.lookups,
            "hit_rate": round(self.hit_rate, 4),
            "evictions": self.evictions,
            "dirty_evictions": self.dirty_evictions,
            "writes_absorbed": self.writes_absorbed,
            "flushes": self.flushes,
            "pages_flushed": self.pages_flushed,
        }


@dataclass(slots=True)
class _Frame:
    """One slot of the pool. Holds bytes, not a decoded page.

    Bytes rather than a ``Page``, because the pool must also hold the meta page,
    which has a different layout entirely, and because decoding is the caller's
    job, so a frame stays valid whatever the page turns out to be.
    """

    frame_id: int
    page_id: int | None = None
    data: bytearray = field(default_factory=bytearray)
    dirty: bool = False
    reads: int = 0
    writes: int = 0
    loaded_at_ns: int = 0

    @property
    def is_free(self) -> bool:
        return self.page_id is None


@dataclass(frozen=True, slots=True)
class FrameSnapshot:
    """One frame, frozen for display."""

    frame_id: int
    page_id: int | None
    dirty: bool
    reads: int
    writes: int
    recency: int
    """0 is the most recently used. ``-1`` for a free frame."""
    resident_for_ns: int


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    """The whole pool at one instant, for the API and the frame grid."""

    capacity: int
    page_size: int
    resident: int
    dirty: int
    frames: tuple[FrameSnapshot, ...]
    stats: BufferPoolStats


class BufferPool:
    """A fixed-size cache of page images, with write-back and LRU eviction.

    Knows nothing about the file. The two callables it is given are the only
    way it reaches storage, which keeps it unit-testable against a dictionary
    and keeps the pager the only thing that touches the disk.
    """

    __slots__ = (
        "_capacity",
        "_frames",
        "_free",
        "_page_size",
        "_read_through",
        "_resident",
        "_stats",
        "_tracer",
        "_write_through",
    )

    def __init__(
        self,
        *,
        page_size: int,
        capacity: int = DEFAULT_POOL_FRAMES,
        read_through: Callable[[int], bytes],
        write_through: Callable[[int, bytes], None],
        tracer: Tracer | None = None,
    ) -> None:
        if capacity < MIN_POOL_FRAMES:
            raise ValueError(
                f"a buffer pool needs at least {MIN_POOL_FRAMES} frames, got {capacity}"
            )
        self._capacity = capacity
        self._page_size = page_size
        self._read_through = read_through
        self._write_through = write_through
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._stats = BufferPoolStats()

        self._frames = [_Frame(frame_id=index) for index in range(capacity)]
        self._free = list(range(capacity - 1, -1, -1))
        #: page id → frame index, in least-recently-used-first order. An
        #: OrderedDict makes both "which is oldest" and "this one was just used"
        #: O(1); see the module docstring on why real systems avoid exact LRU.
        self._resident: OrderedDict[int, int] = OrderedDict()

    # -- properties --------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def stats(self) -> BufferPoolStats:
        return self._stats

    @property
    def resident_pages(self) -> int:
        return len(self._resident)

    @property
    def dirty_pages(self) -> int:
        return sum(1 for frame in self._frames if frame.dirty)

    def contains(self, page_id: int) -> bool:
        return page_id in self._resident

    # -- the hot path ------------------------------------------------------

    def fetch(self, page_id: int) -> bytes:
        """Return a **copy** of the page's bytes, reading it in if necessary.

        A copy, not a view: see the module docstring on why that removes the
        need for pin counts.
        """
        index = self._resident.get(page_id)
        if index is not None:
            frame = self._frames[index]
            frame.reads += 1
            self._resident.move_to_end(page_id)
            self._stats.hits += 1
            self._emit("hit", frame)
            return bytes(frame.data)

        self._stats.misses += 1
        raw = self._read_through(page_id)
        frame = self._admit(page_id, raw, dirty=False)
        frame.reads += 1
        self._emit("miss", frame)
        return bytes(frame.data)

    def store(self, page_id: int, raw: bytes) -> None:
        """Copy ``raw`` into the frame and mark it dirty. **No disk write.**

        The bytes reach the file on eviction or on :meth:`flush`. A page written
        repeatedly is therefore written to disk once, which is where most of the
        pool's benefit comes from. An index build touches one leaf hundreds of
        times.
        """
        if len(raw) != self._page_size:
            raise ValueError(
                f"page {page_id}: refusing to buffer {len(raw)} bytes "
                f"into a {self._page_size}-byte frame"
            )

        index = self._resident.get(page_id)
        if index is not None:
            frame = self._frames[index]
            if frame.dirty:
                # Already dirty, so the previous version was never written. That
                # saved write is the whole point of write-back; count it.
                self._stats.writes_absorbed += 1
            frame.data[:] = raw
            frame.dirty = True
            frame.writes += 1
            self._resident.move_to_end(page_id)
            self._emit("dirty", frame)
            return

        frame = self._admit(page_id, raw, dirty=True)
        frame.writes += 1
        self._emit("dirty", frame)

    def invalidate(self, page_id: int) -> None:
        """Forget a page without writing it back.

        For a page whose contents are about to be overwritten wholesale. A
        freshly allocated page, or one returned to the free list. Writing back
        bytes that are already superseded is pure waste.
        """
        index = self._resident.pop(page_id, None)
        if index is None:
            return
        frame = self._frames[index]
        frame.page_id = None
        frame.dirty = False
        frame.reads = frame.writes = 0
        self._free.append(index)

    # -- admission and eviction -------------------------------------------

    def _admit(self, page_id: int, raw: bytes, *, dirty: bool) -> _Frame:
        index = self._free.pop() if self._free else self._evict()
        frame = self._frames[index]
        frame.page_id = page_id
        frame.data = bytearray(raw)
        frame.dirty = dirty
        frame.reads = frame.writes = 0
        frame.loaded_at_ns = time.monotonic_ns()
        self._resident[page_id] = index
        return frame

    def _evict(self) -> int:
        """Drop the least recently used page and return its frame.

        Never fails: with no pin counts there is always a victim, which is the
        other thing the copy-out design buys. A shared-frame pool has to cope
        with every frame being pinned, and the honest answer there is an error
        rather than corrupting one.
        """
        victim_page, index = self._resident.popitem(last=False)
        frame = self._frames[index]
        self._stats.evictions += 1
        if frame.dirty:
            self._write_through(victim_page, bytes(frame.data))
            self._stats.dirty_evictions += 1
        self._emit("evict", frame)
        frame.page_id = None
        frame.dirty = False
        return index

    # -- durability --------------------------------------------------------

    def flush(self) -> int:
        """Write every dirty frame back. Returns how many were written.

        Called by ``Pager.sync`` *before* the ``fsync``, which is what keeps the
        durability contract identical to the pre-pool engine: after ``sync()``
        returns, everything acknowledged is on disk. Between syncs, more data
        now lives only in memory than before. The crash window is wider, and
        Milestone 9's WAL is what closes it.
        """
        written = 0
        for page_id, index in self._resident.items():
            frame = self._frames[index]
            if not frame.dirty:
                continue
            self._write_through(page_id, bytes(frame.data))
            frame.dirty = False
            written += 1
        self._stats.flushes += 1
        self._stats.pages_flushed += written
        if written and self._tracer.storage:
            self._tracer.emit(
                BufferPoolEvent(
                    action="flush",
                    frame_id=-1,
                    page_id=-1,
                    dirty=False,
                    resident=len(self._resident),
                    pages_written=written,
                )
            )
        return written

    def clear(self, *, flush: bool = True) -> None:
        """Drop everything, flushing first unless told not to.

        ``flush=False`` is the crash simulation: dirty frames are discarded
        rather than written, which is what happens when a process dies.
        """
        if flush:
            self.flush()
        for frame in self._frames:
            frame.page_id = None
            frame.dirty = False
            frame.reads = frame.writes = 0
        self._resident.clear()
        self._free = list(range(self._capacity - 1, -1, -1))

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> PoolSnapshot:
        """Freeze the pool for display.

        Returns plain dataclasses so the API can serialise it after releasing
        the engine lock. The same rule every other diagnostics view follows.
        """
        recency = {
            page_id: position for position, page_id in enumerate(reversed(self._resident))
        }
        now = time.monotonic_ns()
        return PoolSnapshot(
            capacity=self._capacity,
            page_size=self._page_size,
            resident=len(self._resident),
            dirty=self.dirty_pages,
            frames=tuple(
                FrameSnapshot(
                    frame_id=frame.frame_id,
                    page_id=frame.page_id,
                    dirty=frame.dirty,
                    reads=frame.reads,
                    writes=frame.writes,
                    recency=-1 if frame.is_free else recency.get(frame.page_id, -1),
                    resident_for_ns=0 if frame.is_free else now - frame.loaded_at_ns,
                )
                for frame in self._frames
            ),
            # A copy, so the caller holds figures from one instant. slots=True
            # means no __dict__, hence the explicit replace rather than vars().
            stats=replace(self._stats),
        )

    def _emit(self, action: str, frame: _Frame) -> None:
        # Hits are the common case by design, so they are VERBOSE: one event
        # per page read at STORAGE would drown every other event in the stream.
        # A miss or an eviction is rare and interesting, so both are STORAGE.
        wanted = self._tracer.verbose if action == "hit" else self._tracer.storage
        if not wanted:
            return
        self._tracer.emit(
            BufferPoolEvent(
                action=action,  # type: ignore[arg-type]
                frame_id=frame.frame_id,
                page_id=frame.page_id if frame.page_id is not None else -1,
                dirty=frame.dirty,
                resident=len(self._resident),
            )
        )

    def __repr__(self) -> str:
        return (
            f"<BufferPool {len(self._resident)}/{self._capacity} frames "
            f"dirty={self.dirty_pages} hit_rate={self._stats.hit_rate:.0%}>"
        )
