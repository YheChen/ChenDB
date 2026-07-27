"""What a log record looks like on disk.

    ┌────────┬─────┬──────────┬─────┬──────┬─────────┬────────┬───────┬──────┐
    │checksum│ lsn │ prev_lsn │ txn │ type │ page id │ before │ after │images│
    │  u32   │ u64 │   u64    │ u64 │  u8  │   u32   │ len u32│len u32│      │
    └────────┴─────┴──────────┴─────┴──────┴─────────┴────────┴───────┴──────┘
     0        4     12         20    28     32        36       40      44

The LSN **is the record's byte offset in the log stream**, not a separate
counter. That is what PostgreSQL does — an LSN there is literally a position in
the WAL — and it makes two otherwise-fiddly things free:

* "the log is durable up to LSN *n*" means "the first *n* bytes are on disk",
  which is a comparison rather than a lookup;
* a record's LSN can be computed before it is written, which the pager needs:
  a page carries the LSN of the record describing it, so the record has to know
  its own LSN before it encodes that page image inside itself.

Whole pages, not deltas
-----------------------
An ``UPDATE`` record carries the entire page after the change, and — the first
time a transaction touches a page — the entire page before it. That is the same
choice Milestone 8's undo log made, for the same reason: the log never has to
know what a heap row, a B+ tree node or a catalog tuple *is*.

It is also this milestone's real cost, and it is not small. A hot page written a
thousand times logs a thousand 4 KiB images. Real systems log *physiologically*
— "insert this tuple into page 7" — which is a few dozen bytes, at the price of
a redo routine per operation that has to be exactly the inverse of the operation
itself. PostgreSQL splits the difference: a full-page image the first time a
page changes after a checkpoint, protecting against torn writes, and deltas
after. ``docs/milestone-09-wal.md`` measures what the simple choice costs here.

Every record is checksummed, and that is what makes a torn tail detectable:
recovery scans forward until a record fails to decode, and stops. A
half-written record at the end of a log is the normal state after a crash, not
corruption.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

__all__ = [
    "NO_TRANSACTION",
    "RECORD_HEADER_FORMAT",
    "RECORD_HEADER_SIZE",
    "LogRecord",
    "RecordType",
    "decode_record",
]


class RecordType(IntEnum):
    """What a record says happened.

    There is no ``BEGIN``. Analysis learns a transaction exists from its first
    ``UPDATE``, so a begin record would be one more write per transaction
    carrying information already implied — PostgreSQL leaves it out for the
    same reason.
    """

    UPDATE = 1
    """A page changed. Carries the after-image, and the before-image if this
    was the transaction's first write to that page."""

    COMMIT = 2
    """Everything this transaction logged counts. This record is the only
    durable fact separating a finished transaction from an interrupted one, and
    writing it is what ``COMMIT`` now means."""

    ABORT = 3
    """This transaction was rolled back, and the restores are already in the log
    as ordinary ``UPDATE`` records — because rollback writes pages through the
    same path everything else does. So recovery treats an aborted transaction
    exactly like a committed one: replay what is there and stop."""

    CHECKPOINT = 4
    """Every page dirty before this point is now on disk, so recovery may start
    here rather than at the beginning of time."""


#: checksum u32 · lsn u64 · prev_lsn u64 · transaction u64 · type u8 ·
#: flags u8 · reserved u16 · page_id u32 · before_len u32 · after_len u32
RECORD_HEADER_FORMAT: Final[str] = "<IQQQBBHIII"
RECORD_HEADER_SIZE: Final[int] = struct.calcsize(RECORD_HEADER_FORMAT)  # 44

#: The checksum is first and covers everything after it, so it protects the two
#: lengths that say how far the record extends. A corrupt length that slipped
#: past the checksum would walk the scanner off the end of the file.
_CHECKSUM_COVERAGE_START: Final = 4

#: A record belonging to no transaction: engine bookkeeping that happens outside
#: one, such as the meta page written when a database is created. Recovery
#: replays these unconditionally — there is nobody to roll them back.
NO_TRANSACTION: Final = 0

#: Sanity bound on a decoded length, so a corrupt header cannot make the scanner
#: try to allocate the whole file. Nothing legitimate exceeds one page.
_MAX_IMAGE_BYTES: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One entry in the log."""

    lsn: int
    """This record's byte offset in the log stream."""
    prev_lsn: int
    """The previous record of the same transaction, or 0 for its first.

    ARIES calls this the backward chain and uses it to walk a loser's updates in
    reverse without touching the rest of the log. ChenDB's recovery scans anyway
    — these logs are small enough that a second pass costs less than maintaining
    an index would — so this is here for the visualizer, which draws the chain,
    and because leaving it out would misrepresent what an ARIES record is.
    """
    transaction_id: int
    record_type: RecordType
    page_id: int = 0
    before_image: bytes = b""
    after_image: bytes = b""

    @property
    def size(self) -> int:
        return RECORD_HEADER_SIZE + len(self.before_image) + len(self.after_image)

    @property
    def end_lsn(self) -> int:
        """One past this record — the LSN the next record will be given."""
        return self.lsn + self.size

    @property
    def has_undo(self) -> bool:
        """True when this record alone is enough to roll its page back."""
        return bool(self.before_image)

    def to_bytes(self) -> bytes:
        buf = bytearray(self.size)
        struct.pack_into(
            RECORD_HEADER_FORMAT,
            buf,
            0,
            0,  # checksum, filled in below
            self.lsn,
            self.prev_lsn,
            self.transaction_id,
            int(self.record_type),
            0,  # flags
            0,  # reserved
            self.page_id,
            len(self.before_image),
            len(self.after_image),
        )
        cut = RECORD_HEADER_SIZE + len(self.before_image)
        buf[RECORD_HEADER_SIZE:cut] = self.before_image
        buf[cut:] = self.after_image
        struct.pack_into(
            "<I", buf, 0, zlib.crc32(memoryview(buf)[_CHECKSUM_COVERAGE_START:])
        )
        return bytes(buf)

    def __repr__(self) -> str:
        page = f" page={self.page_id}" if self.record_type is RecordType.UPDATE else ""
        return (
            f"<{self.record_type.name} lsn={self.lsn} "
            f"txn={self.transaction_id}{page} {self.size}B>"
        )


def decode_record(raw: bytes, offset: int, base_lsn: int = 0) -> LogRecord | None:
    """Decode one record at ``offset``, or ``None`` if it is not whole.

    ``None`` rather than an exception, because an incomplete or corrupt record at
    the end of a log is the *expected* state after a crash: the process died
    part-way through a write. Recovery stops at the first ``None`` and treats
    everything before it as the log.

    A record that fails its checksum in the *middle* of a log would be real
    corruption rather than a torn tail — and the two are indistinguishable
    without more machinery than this earns, so both stop the scan and the
    truncation is reported rather than hidden.

    ``base_lsn`` is the LSN of the log file's first byte, which is non-zero
    after a checkpoint has truncated it. The stored LSN is checked against the
    position it was found at: a record claiming an LSN it is not sitting at
    means the file has been spliced, and is not something to replay.
    """
    if offset + RECORD_HEADER_SIZE > len(raw):
        return None
    (
        checksum,
        lsn,
        prev_lsn,
        transaction_id,
        type_value,
        _flags,
        _reserved,
        page_id,
        before_len,
        after_len,
    ) = struct.unpack_from(RECORD_HEADER_FORMAT, raw, offset)

    if before_len > _MAX_IMAGE_BYTES or after_len > _MAX_IMAGE_BYTES:
        return None
    end = offset + RECORD_HEADER_SIZE + before_len + after_len
    if end > len(raw):
        return None
    if zlib.crc32(memoryview(raw)[offset + _CHECKSUM_COVERAGE_START : end]) != checksum:
        return None
    if lsn != base_lsn + offset:
        return None
    try:
        record_type = RecordType(type_value)
    except ValueError:
        return None

    body = offset + RECORD_HEADER_SIZE
    return LogRecord(
        lsn=lsn,
        prev_lsn=prev_lsn,
        transaction_id=transaction_id,
        record_type=record_type,
        page_id=page_id,
        before_image=bytes(raw[body : body + before_len]),
        after_image=bytes(raw[body + before_len : end]),
    )
