"""The slotted page: ChenDB's unit of storage.

A *slotted page* stores variable-length records inside a fixed-size block by
splitting the block into two regions that grow toward each other::

     0                                                        page_size
     ├───────────┬────────────────┬──────────────┬────────────────────┤
     │  header   │ slot directory │  free space  │    record data     │
     │   24 B    │  4 B per slot  │              │                    │
     └───────────┴────────────────┴──────────────┴────────────────────┘
                 ↑                ↑              ↑
                 24        free_start        free_end            page_size
                           (grows →)         (grows ←)

The slot directory is an array of ``(offset, length)`` pairs.  Callers never
hold a byte offset; they hold a *slot index*.  That one level of indirection is
the entire point of the design:

* records can be moved within the page (compaction) without invalidating any
  external reference, because only the slot entry changes;
* records are variable length, yet lookup by slot is O(1);
* deletion is O(1) — write a tombstone into the slot entry.

PostgreSQL's ``PageHeaderData`` + ``ItemIdData`` array is the same structure.
Its ``pd_lower``/``pd_upper`` are our ``free_start``/``free_end``, and its
4-byte ``ItemIdData`` bit-packs ``lp_off:15, lp_flags:2, lp_len:15`` where we
use two plain uint16 fields.  SQLite also uses a slotted layout, but its cell
pointer array holds only offsets — cell lengths are re-derived by parsing the
cell header, which saves 2 bytes per row at the cost of CPU on every access.

Complexity
----------
========================  ==========================================
Operation                 Cost
========================  ==========================================
``insert``                O(1) amortised; O(slots) when it compacts
``read(slot)``            O(1)
``delete(slot)``          O(1)
``compact``               O(slots + live bytes)
``iter_records``          O(slots)
========================  ==========================================

The in-memory representation *is* the on-disk representation: a ``Page`` wraps
a ``bytearray`` of exactly ``page_size`` bytes and decodes header fields on
demand.  There is no separate "parsed" form to keep in sync, and Milestone 7's
buffer pool can therefore hand out pages without any conversion step.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from engine.errors import ChecksumMismatchError, CorruptPageError, RecordTooLargeError
from engine.storage.constants import (
    DEFAULT_PAGE_SIZE,
    INVALID_PAGE_ID,
    MAX_PAGE_SIZE,
    MIN_PAGE_SIZE,
    PageType,
)

__all__ = [
    "PAGE_HEADER_FORMAT",
    "PAGE_HEADER_SIZE",
    "SLOT_FORMAT",
    "SLOT_SIZE",
    "Page",
    "SlotInfo",
]

# checksum:u32  lsn:u64  page_type:u8  flags:u8
# slot_count:u16  free_start:u16  free_end:u16  next_page_id:u32
PAGE_HEADER_FORMAT: Final[str] = "<IQBBHHHI"
PAGE_HEADER_SIZE: Final[int] = struct.calcsize(PAGE_HEADER_FORMAT)  # 24

SLOT_FORMAT: Final[str] = "<HH"  # offset:u16  length:u16
SLOT_SIZE: Final[int] = struct.calcsize(SLOT_FORMAT)  # 4

# Byte offsets of individual header fields, so accessors can patch one field
# without re-encoding the whole header.
_OFF_CHECKSUM: Final = 0
_OFF_LSN: Final = 4
_OFF_PAGE_TYPE: Final = 12
_OFF_FLAGS: Final = 13
_OFF_SLOT_COUNT: Final = 14
_OFF_FREE_START: Final = 16
_OFF_FREE_END: Final = 18
_OFF_NEXT_PAGE: Final = 20

_U8: Final = struct.Struct("<B")
_U16: Final = struct.Struct("<H")
_U32: Final = struct.Struct("<I")
_U64: Final = struct.Struct("<Q")
_SLOT: Final = struct.Struct(SLOT_FORMAT)

#: A slot entry of ``(0, 0)`` marks a dead record.  Offset 0 is unambiguous as
#: a tombstone because a live payload always starts after the header.
_DEAD_OFFSET: Final = 0
_DEAD_LENGTH: Final = 0

#: Bytes covered by the page checksum: everything after the checksum field.
_CHECKSUM_COVERAGE_START: Final = 4


@dataclass(frozen=True, slots=True)
class SlotInfo:
    """One entry of the slot directory, decoded for inspection.

    Produced by :meth:`Page.slot_directory` and consumed by tests and by the
    visualizer's page inspector.  It is a read-only view — mutating a page goes
    through :meth:`Page.insert` / :meth:`Page.delete`.
    """

    slot_id: int
    offset: int
    length: int

    @property
    def is_live(self) -> bool:
        return self.offset != _DEAD_OFFSET


class Page:
    """A fixed-size block of bytes with a slotted-record layout.

    A ``Page`` owns its buffer.  Mutating methods write straight into that
    buffer; nothing is persisted until a :class:`~engine.storage.pager.Pager`
    writes the page back to disk.
    """

    __slots__ = ("_buf", "_page_size", "page_id")

    def __init__(self, page_id: int, buf: bytearray, page_size: int) -> None:
        if len(buf) != page_size:
            raise CorruptPageError(
                f"page {page_id}: buffer is {len(buf)} bytes, expected {page_size}"
            )
        self.page_id = page_id
        self._buf = buf
        self._page_size = page_size

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        page_id: int,
        page_type: PageType,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page:
        """Build a new, empty page of ``page_type``."""
        validate_page_size(page_size)
        page = cls(page_id, bytearray(page_size), page_size)
        page._set_u8(_OFF_PAGE_TYPE, int(page_type))
        page.slot_count = 0
        page.free_start = PAGE_HEADER_SIZE
        page.free_end = page_size
        page.next_page_id = INVALID_PAGE_ID
        return page

    @classmethod
    def from_bytes(
        cls,
        page_id: int,
        raw: bytes | bytearray,
        page_size: int = DEFAULT_PAGE_SIZE,
        *,
        verify_checksum: bool = True,
        validate: bool = True,
    ) -> Page:
        """Decode a page, checking its invariants unless told not to.

        ``validate`` runs :meth:`validate_header` — the O(1) checks — not the
        full slot walk, which is 13 µs on a full 4 KiB page and belongs to the
        inspector rather than the read path.

        Both default on, because the caller that reads from disk must make them.
        The buffer pool is the one caller that turns them off: a resident page
        was verified when it was admitted, or its bytes came from
        :meth:`to_bytes`, which recomputes the checksum.
        """
        page = cls(page_id, bytearray(raw), page_size)
        if verify_checksum:
            page.verify_checksum()
        if validate:
            page.validate_header()
        return page

    def to_bytes(self) -> bytes:
        """Return an immutable copy of the page buffer, checksum refreshed."""
        self.update_checksum()
        return bytes(self._buf)

    # -- raw field accessors ----------------------------------------------

    def _get_u8(self, off: int) -> int:
        return _U8.unpack_from(self._buf, off)[0]

    def _set_u8(self, off: int, value: int) -> None:
        _U8.pack_into(self._buf, off, value)

    def _get_u16(self, off: int) -> int:
        return _U16.unpack_from(self._buf, off)[0]

    def _set_u16(self, off: int, value: int) -> None:
        _U16.pack_into(self._buf, off, value)

    def _get_u32(self, off: int) -> int:
        return _U32.unpack_from(self._buf, off)[0]

    def _set_u32(self, off: int, value: int) -> None:
        _U32.pack_into(self._buf, off, value)

    # -- header ------------------------------------------------------------

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def page_type(self) -> PageType:
        raw = self._get_u8(_OFF_PAGE_TYPE)
        try:
            return PageType(raw)
        except ValueError as exc:
            raise CorruptPageError(
                f"page {self.page_id}: unknown page type {raw}"
            ) from exc

    @page_type.setter
    def page_type(self, value: PageType) -> None:
        self._set_u8(_OFF_PAGE_TYPE, int(value))

    @property
    def flags(self) -> int:
        return self._get_u8(_OFF_FLAGS)

    @flags.setter
    def flags(self, value: int) -> None:
        self._set_u8(_OFF_FLAGS, value)

    @property
    def lsn(self) -> int:
        """Log sequence number of the last change to this page.

        Unused until Milestone 9.  Recovery compares it against the WAL to
        decide whether a logged change is already reflected on the page.
        """
        return _U64.unpack_from(self._buf, _OFF_LSN)[0]

    @lsn.setter
    def lsn(self, value: int) -> None:
        _U64.pack_into(self._buf, _OFF_LSN, value)

    @property
    def slot_count(self) -> int:
        """Number of slot-directory entries, including tombstones."""
        return self._get_u16(_OFF_SLOT_COUNT)

    @slot_count.setter
    def slot_count(self, value: int) -> None:
        self._set_u16(_OFF_SLOT_COUNT, value)

    @property
    def free_start(self) -> int:
        """End of the slot directory (PostgreSQL's ``pd_lower``)."""
        return self._get_u16(_OFF_FREE_START)

    @free_start.setter
    def free_start(self, value: int) -> None:
        self._set_u16(_OFF_FREE_START, value)

    @property
    def free_end(self) -> int:
        """Start of the record-data region (PostgreSQL's ``pd_upper``)."""
        return self._get_u16(_OFF_FREE_END)

    @free_end.setter
    def free_end(self, value: int) -> None:
        self._set_u16(_OFF_FREE_END, value)

    @property
    def next_page_id(self) -> int:
        """Next page in the owning heap's chain, or ``INVALID_PAGE_ID``."""
        return self._get_u32(_OFF_NEXT_PAGE)

    @next_page_id.setter
    def next_page_id(self, value: int) -> None:
        self._set_u32(_OFF_NEXT_PAGE, value)

    @property
    def checksum(self) -> int:
        return self._get_u32(_OFF_CHECKSUM)

    # -- checksum ----------------------------------------------------------

    def compute_checksum(self) -> int:
        """CRC32 over every byte except the checksum field itself."""
        return zlib.crc32(memoryview(self._buf)[_CHECKSUM_COVERAGE_START:])

    def update_checksum(self) -> None:
        """Recompute and store the checksum. Called on every write."""
        self._set_u32(_OFF_CHECKSUM, self.compute_checksum())

    def verify_checksum(self) -> None:
        """Raise :class:`ChecksumMismatchError` if the page is damaged.

        A mismatch means the page on disk differs from the page that was
        written: a torn write, a partial flush, or media corruption.  Detecting
        it is why PostgreSQL ships ``data_checksums`` and why ZFS checksums
        every block.
        """
        stored = self.checksum
        actual = self.compute_checksum()
        if stored != actual:
            raise ChecksumMismatchError(
                f"page {self.page_id}: checksum mismatch "
                f"(stored 0x{stored:08x}, computed 0x{actual:08x})"
            )

    # -- space accounting --------------------------------------------------

    @property
    def max_payload_size(self) -> int:
        """Largest record this page type can ever hold (one slot, empty page)."""
        return self._page_size - PAGE_HEADER_SIZE - SLOT_SIZE

    @property
    def free_space(self) -> int:
        """Contiguous unused bytes between the slot directory and the records."""
        return self.free_end - self.free_start

    @property
    def reclaimable_space(self) -> int:
        """Bytes that :meth:`compact` would return to the free region.

        This is space held by tombstoned records plus trailing dead slots.  It
        is *not* contiguous, so it cannot satisfy an insert until compaction
        runs.  PostgreSQL exposes the same distinction through
        ``pg_freespace`` and its opportunistic "page pruning".
        """
        live_bytes = 0
        trailing_dead = 0
        counting_trailing = True
        for slot_id in reversed(range(self.slot_count)):
            offset, length = self._read_slot(slot_id)
            if offset == _DEAD_OFFSET:
                if counting_trailing:
                    trailing_dead += 1
                continue
            counting_trailing = False
            live_bytes += length
        record_region = self._page_size - self.free_end
        return (record_region - live_bytes) + trailing_dead * SLOT_SIZE

    def validate_header(self) -> None:
        """Check the O(1) invariants: the free-space pointers agree.

        Split out from :meth:`validate` because this is what a page read can
        afford. Walking the slot directory costs one ``struct`` unpack per slot
        — **13 µs on a full 4 KiB page**, measured, against 100 ns for the
        checksum — so doing it per read made the buffer pool nearly pointless.

        PostgreSQL draws the same line: it verifies the checksum and a few
        header fields on read, and never walks the line pointer array to prove
        it is self-consistent. A page that passes its checksum but has bad slots
        is an engine bug, not media corruption, and a per-read scan is a poor
        way to find one — :meth:`validate` is called explicitly by the page
        inspector and by the tests, which is where it earns its cost.
        """
        expected_free_start = PAGE_HEADER_SIZE + self.slot_count * SLOT_SIZE
        if self.free_start != expected_free_start:
            raise CorruptPageError(
                f"page {self.page_id}: free_start={self.free_start} but "
                f"{self.slot_count} slots imply {expected_free_start}"
            )
        if not expected_free_start <= self.free_end <= self._page_size:
            raise CorruptPageError(
                f"page {self.page_id}: free_end={self.free_end} outside "
                f"[{expected_free_start}, {self._page_size}]"
            )

    def validate(self) -> None:
        """Check every invariant, including one pass over the slot directory.

        O(slots), so not on the read path. See :meth:`validate_header`.
        """
        self.validate_header()
        slot_count = self.slot_count
        for slot_id in range(slot_count):
            offset, length = self._read_slot(slot_id)
            if offset == _DEAD_OFFSET:
                continue
            if offset < self.free_end or offset + length > self._page_size:
                raise CorruptPageError(
                    f"page {self.page_id}: slot {slot_id} points to "
                    f"[{offset}, {offset + length}) outside the record region "
                    f"[{self.free_end}, {self._page_size})"
                )

    # -- slot directory ----------------------------------------------------

    def _slot_offset(self, slot_id: int) -> int:
        return PAGE_HEADER_SIZE + slot_id * SLOT_SIZE

    def _read_slot(self, slot_id: int) -> tuple[int, int]:
        return _SLOT.unpack_from(self._buf, self._slot_offset(slot_id))

    def _write_slot(self, slot_id: int, offset: int, length: int) -> None:
        _SLOT.pack_into(self._buf, self._slot_offset(slot_id), offset, length)

    def _find_dead_slot(self) -> int | None:
        """First tombstoned slot, so slot ids get reused instead of growing."""
        for slot_id in range(self.slot_count):
            if self._read_slot(slot_id)[0] == _DEAD_OFFSET:
                return slot_id
        return None

    def slot_directory(self) -> tuple[SlotInfo, ...]:
        """Decode every slot entry, live and dead, in slot order."""
        return tuple(
            SlotInfo(slot_id, *self._read_slot(slot_id))
            for slot_id in range(self.slot_count)
        )

    # -- record operations -------------------------------------------------

    def space_needed_for(self, length: int) -> int:
        """Bytes of free space an insert of ``length`` requires right now.

        A tombstoned slot can be reused, in which case the slot directory does
        not grow and only the payload needs room.
        """
        if self._find_dead_slot() is not None:
            return length
        return length + SLOT_SIZE

    def would_fit(self, length: int) -> bool:
        """Whether a record of ``length`` fits without compacting."""
        return self.space_needed_for(length) <= self.free_space

    def would_fit_after_compaction(self, length: int) -> bool:
        """Whether compacting this page would make room for ``length``."""
        return self.space_needed_for(length) <= self.free_space + self.reclaimable_space

    def insert(self, payload: bytes) -> int | None:
        """Store ``payload`` and return its slot id, or ``None`` if it will not fit.

        Only the contiguous free region is considered — this never compacts.
        Deciding between "compact this page" and "move to another page" is a
        storage *policy* question, so it belongs to the heap, not here.

        Returning ``None`` rather than raising is deliberate: "this page is
        full" is the expected control flow that makes the heap allocate another
        page, not an error.  A payload too large for *any* page does raise.
        """
        length = len(payload)
        if length > self.max_payload_size:
            raise RecordTooLargeError(
                f"record of {length} bytes exceeds the {self.max_payload_size}-byte "
                f"limit for a {self._page_size}-byte page"
            )

        slot_id = self._find_dead_slot()
        needed = length if slot_id is not None else length + SLOT_SIZE
        if needed > self.free_space:
            return None

        new_free_end = self.free_end - length
        self._buf[new_free_end : new_free_end + length] = payload
        if slot_id is None:
            slot_id = self.slot_count
            self.slot_count = slot_id + 1
            self.free_start = self.free_start + SLOT_SIZE
        self._write_slot(slot_id, new_free_end, length)
        self.free_end = new_free_end
        return slot_id

    # -- ordered slot operations (index pages only) ------------------------
    #
    # A heap addresses records by slot id, so a slot id must never change
    # meaning: the two methods below shift the slot directory and therefore
    # *renumber* every slot after the one they touch.  That is exactly what a
    # B+ tree node wants — its entries must sit in key order, and nothing
    # outside the node holds a reference to slot 4 in particular — and exactly
    # what a heap must never do, because every ``RecordId`` in the database
    # would silently start pointing at the wrong row.
    #
    # PostgreSQL draws the same line: ``PageIndexTupleDelete`` compacts the line
    # pointer array and is used only on index pages, while heap pages get
    # ``ItemIdSetDead``, which leaves the pointer in place.

    def insert_at(self, index: int, payload: bytes) -> bool:
        """Insert ``payload`` so it occupies slot ``index``, shifting the rest up.

        Returns ``False`` if it does not fit without compacting, mirroring
        :meth:`insert`.  **Renumbers slots** — see the note above.
        """
        length = len(payload)
        if length > self.max_payload_size:
            raise RecordTooLargeError(
                f"index entry of {length} bytes exceeds the {self.max_payload_size}-byte "
                f"limit for a {self._page_size}-byte page"
            )
        slot_count = self.slot_count
        if not 0 <= index <= slot_count:
            raise CorruptPageError(
                f"page {self.page_id}: slot index {index} outside [0, {slot_count}]"
            )
        # Always grows the directory: an ordered page has no tombstones to reuse
        # because delete_at removes the entry outright.
        if length + SLOT_SIZE > self.free_space:
            return False

        new_free_end = self.free_end - length
        self._buf[new_free_end : new_free_end + length] = payload

        directory_start = self._slot_offset(index)
        directory_end = self._slot_offset(slot_count)
        # One memmove of the tail, then overwrite the vacated entry.
        self._buf[directory_start + SLOT_SIZE : directory_end + SLOT_SIZE] = self._buf[
            directory_start:directory_end
        ]
        self.slot_count = slot_count + 1
        self.free_start = self.free_start + SLOT_SIZE
        self._write_slot(index, new_free_end, length)
        self.free_end = new_free_end
        return True

    def delete_at(self, index: int) -> bool:
        """Remove slot ``index`` entirely, shifting later slots down.

        The payload bytes stay where they are until :meth:`compact` runs; only
        the directory shrinks.  **Renumbers slots** — see the note above.
        """
        slot_count = self.slot_count
        if not 0 <= index < slot_count:
            return False
        directory_start = self._slot_offset(index)
        directory_end = self._slot_offset(slot_count)
        self._buf[directory_start : directory_end - SLOT_SIZE] = self._buf[
            directory_start + SLOT_SIZE : directory_end
        ]
        new_free_start = self.free_start - SLOT_SIZE
        self._buf[new_free_start : self.free_start] = bytes(SLOT_SIZE)
        self.slot_count = slot_count - 1
        self.free_start = new_free_start
        return True

    def clear_records(self) -> None:
        """Drop every slot and every payload, keeping the page's type and links.

        Used when a node is rebuilt from a new entry list after a split. Zeroing
        rather than reusing means a hexdump of a split node never shows half of
        the pre-split contents, which would be baffling in the page inspector.
        """
        self._buf[PAGE_HEADER_SIZE : self._page_size] = bytes(
            self._page_size - PAGE_HEADER_SIZE
        )
        self.slot_count = 0
        self.free_start = PAGE_HEADER_SIZE
        self.free_end = self._page_size

    def read(self, slot_id: int) -> bytes | None:
        """Return the payload in ``slot_id``, or ``None`` if the slot is dead."""
        if not 0 <= slot_id < self.slot_count:
            return None
        offset, length = self._read_slot(slot_id)
        if offset == _DEAD_OFFSET:
            return None
        return bytes(self._buf[offset : offset + length])

    def delete(self, slot_id: int) -> bool:
        """Tombstone ``slot_id``. Returns ``False`` if it was already dead.

        The payload bytes are left in place; only the slot entry is cleared.
        Space is returned to the page by :meth:`compact`, exactly as PostgreSQL
        defers reclamation to page pruning and ``VACUUM``.
        """
        if not 0 <= slot_id < self.slot_count:
            return False
        offset, _ = self._read_slot(slot_id)
        if offset == _DEAD_OFFSET:
            return False
        self._write_slot(slot_id, _DEAD_OFFSET, _DEAD_LENGTH)
        return True

    def iter_records(self) -> Iterator[tuple[int, bytes]]:
        """Yield ``(slot_id, payload)`` for every live slot, in slot order."""
        for slot_id in range(self.slot_count):
            offset, length = self._read_slot(slot_id)
            if offset == _DEAD_OFFSET:
                continue
            yield slot_id, bytes(self._buf[offset : offset + length])

    @property
    def live_record_count(self) -> int:
        return sum(
            1
            for slot_id in range(self.slot_count)
            if self._read_slot(slot_id)[0] != _DEAD_OFFSET
        )

    # -- maintenance -------------------------------------------------------

    def compact(self) -> int:
        """Slide live records together, reclaiming tombstoned space.

        Returns the number of bytes added to the free region.  **Slot ids are
        preserved** — only the offsets inside slot entries change — so any
        ``RecordId`` held elsewhere in the system stays valid.  That property
        is the reason the slot directory exists at all.
        """
        before = self.free_space
        live = [
            (slot_id, bytes(self._buf[offset : offset + length]))
            for slot_id in range(self.slot_count)
            for offset, length in (self._read_slot(slot_id),)
            if offset != _DEAD_OFFSET
        ]

        # Zero the old record region so stale bytes never leak into a hexdump
        # or, worse, into a page written back to disk.
        self._buf[self.free_end : self._page_size] = bytes(
            self._page_size - self.free_end
        )

        cursor = self._page_size
        for slot_id, payload in live:
            cursor -= len(payload)
            self._buf[cursor : cursor + len(payload)] = payload
            self._write_slot(slot_id, cursor, len(payload))
        self.free_end = cursor

        self._trim_trailing_dead_slots()
        return self.free_space - before

    def _trim_trailing_dead_slots(self) -> None:
        """Shrink the slot directory past any tombstones at its tail.

        Only *trailing* entries can go: removing an interior slot would
        renumber every slot after it and invalidate outstanding record ids.
        """
        new_count = self.slot_count
        while new_count > 0 and self._read_slot(new_count - 1)[0] == _DEAD_OFFSET:
            new_count -= 1
        if new_count == self.slot_count:
            return
        new_free_start = PAGE_HEADER_SIZE + new_count * SLOT_SIZE
        self._buf[new_free_start : self.free_start] = bytes(
            self.free_start - new_free_start
        )
        self.slot_count = new_count
        self.free_start = new_free_start

    # -- debugging ---------------------------------------------------------

    @property
    def raw(self) -> memoryview:
        """Read-only view of the underlying buffer, for hexdumps and tests."""
        return memoryview(self._buf).toreadonly()

    def __repr__(self) -> str:
        return (
            f"<Page id={self.page_id} type={self.page_type.name} "
            f"slots={self.slot_count} live={self.live_record_count} "
            f"free={self.free_space}B next={_format_page_id(self.next_page_id)}>"
        )


def validate_page_size(page_size: int) -> None:
    """Reject page sizes the format cannot represent."""
    if not MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(
            f"page_size {page_size} outside [{MIN_PAGE_SIZE}, {MAX_PAGE_SIZE}]"
        )
    if page_size & (page_size - 1):
        raise ValueError(f"page_size {page_size} must be a power of two")


def _format_page_id(page_id: int) -> str:
    return "-" if page_id == INVALID_PAGE_ID else str(page_id)
