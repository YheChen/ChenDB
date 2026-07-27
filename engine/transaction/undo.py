"""The undo log: how a transaction takes back what it did.

    INSERT INTO users VALUES (1, 'ada')
        │
        ├─ page 4 is about to change
        │     └─ no before-image yet?  save a copy of page 4 as it is now
        │
        └─ write the new page 4

    ROLLBACK
        │
        └─ write every saved before-image back, newest first

Physical, not logical
---------------------
An undo record here is **a copy of a whole page as it was before the transaction
first touched it**.  The alternative — a *logical* record saying "this insert
added row 7, so delete row 7" — produces far smaller records, and was rejected
for one reason:

A logical undo has to know what every write *meant*.  Undoing an insert means
deleting the heap row **and** removing its entry from every index; undoing a
``CREATE TABLE`` means deleting rows from two system tables and returning a heap
page to the free list; undoing a B+ tree split means merging two nodes, which
:mod:`engine.index.bplustree` deliberately cannot do.  Each of those is a
separate piece of reasoning, and each is a separate chance to get it wrong in a
way that only shows up as corruption.

A physical undo knows nothing.  It restores bytes.  That makes it correct
across the heap, the indexes, the catalog and the meta page *uniformly*, with no
per-subsystem code — which is exactly why ``CREATE TABLE`` became atomic in this
milestone without anything in the catalog changing.

Real systems land in between. ARIES calls it **physiological** logging: physical
to a page, logical within one, so a record says "insert this tuple into page 7"
rather than "here are all 8192 bytes of page 7". PostgreSQL writes full-page
images too, but only for the first change to a page after a checkpoint, for
exactly the torn-write reason ChenDB's checksums exist for.

What it costs
-------------
One page image per page touched, **not** per write: the log keeps only the
*first* before-image of each page, because restoring that undoes every later
change to it as well. A transaction that appends a thousand rows to one heap
page therefore holds one 4 KiB image, not a thousand.

That bounds the log at ``pages touched x page_size``. A transaction that
rewrites a 10,000-page table would hold 40 MB in memory, so
:data:`MAX_UNDO_BYTES` stops it growing past a ceiling.

Since Milestone 9 that ceiling is a **memory** bound and no longer a
correctness one. The write-ahead log carries the same before-images, on disk,
under the same first-write-wins rule — so a transaction that overflows this log
simply stops caching and :meth:`Database.rollback` reads what it needs from the
WAL instead. This class is the fast path, not the only path. That is what
Oracle's undo tablespace and InnoDB's rollback segments are: undo that lives on
disk so a long transaction is bounded by disk rather than RAM.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

__all__ = ["MAX_UNDO_BYTES", "UndoLog", "UndoRecord"]

#: Ceiling on one transaction's undo log. 64 MiB is 16,000 pages at 4 KiB —
#: far more than any teaching workload, and small enough that a runaway
#: transaction fails with a clear message instead of swapping the machine.
MAX_UNDO_BYTES: Final = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class UndoRecord:
    """One page as it was before this transaction first changed it."""

    sequence: int
    """Order within the transaction. Rollback replays in reverse."""
    page_id: int
    before_image: bytes
    reason: str
    """What was about to happen, for the transaction view. Not used to undo —
    the bytes are the whole mechanism — but a log of "page 4, page 4, page 1"
    is unreadable without it."""

    @property
    def size(self) -> int:
        return len(self.before_image)


@dataclass(slots=True)
class UndoLog:
    """Before-images for one transaction, one per page touched."""

    _records: list[UndoRecord] = field(default_factory=list)
    _captured: set[int] = field(default_factory=set)
    _bytes: int = 0
    overflowed: bool = False
    """This transaction touched more than :data:`MAX_UNDO_BYTES` of pages, so
    the in-memory copy is incomplete. Rollback reads the WAL instead."""

    def has(self, page_id: int) -> bool:
        return page_id in self._captured

    def capture(self, page_id: int, image: bytes, reason: str = "") -> bool:
        """Save ``image`` unless this page is already covered.

        Returns whether a record was actually added, so the caller can avoid
        reading a page it does not need. First-write-wins is what keeps the log
        proportional to *pages touched* rather than to *writes*.
        """
        if page_id in self._captured:
            return False
        # The page is marked captured either way. That is the important part:
        # first-write-wins is a *decision*, and it has to hold whether or not
        # there is room to keep the bytes, because the WAL asks the same
        # question through :meth:`has` and must get the same answer. Letting the
        # decision lapse under memory pressure would have the log record a fresh
        # "before" on every later write to the page — mid-transaction states,
        # each claiming to be the state to roll back to.
        #
        # A page id is 8 bytes against a 4 KiB image, so the set is not what
        # runs out of room; only the images are.
        self._captured.add(page_id)
        if self._bytes + len(image) > MAX_UNDO_BYTES:
            # Stop caching rather than fail. The write-ahead log has this same
            # image on disk, so a rollback is still exact — it just reads the
            # log instead of memory. Raising would have made this ceiling a
            # limit on transaction *size*, which it is not.
            self.overflowed = True
            return False
        self._records.append(
            UndoRecord(
                sequence=len(self._records),
                page_id=page_id,
                before_image=bytes(image),
                reason=reason,
            )
        )
        self._bytes += len(image)
        return True

    def rewind(self) -> Iterator[UndoRecord]:
        """Records newest-first, which is the order rollback must apply them.

        Order does not strictly matter while there is one image per page — no
        two records touch the same page — but reverse is the order that stays
        correct if that ever changes, and it is what every real undo does.
        """
        return reversed(self._records)

    def records(self) -> tuple[UndoRecord, ...]:
        return tuple(self._records)

    @property
    def page_count(self) -> int:
        return len(self._records)

    @property
    def bytes_held(self) -> int:
        return self._bytes

    def clear(self) -> None:
        self._records.clear()
        self._captured.clear()
        self._bytes = 0
        self.overflowed = False

    def __len__(self) -> int:
        return len(self._records)

    def __repr__(self) -> str:
        return f"<UndoLog {len(self._records)} pages, {self._bytes} bytes>"
