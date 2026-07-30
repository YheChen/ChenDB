"""B+ tree nodes, built on the ordinary slotted page.

A node is not a new kind of storage — it is a :class:`~engine.storage.page.Page`
whose ``page_type`` is ``BTREE_LEAF`` or ``BTREE_INTERNAL`` and whose slots hold
index entries instead of table rows.  Reusing the page means splits, checksums,
the free list, the page inspector and the disk map all work on index pages the
day they are written, with no new code.

Layout
------
::

    leaf page                                    internal page
    ┌──────────────────────────────┐             ┌──────────────────────────────┐
    │ header (24 B)                │             │ header (24 B)                │
    │   page_type = BTREE_LEAF     │             │   page_type = BTREE_INTERNAL │
    │   next_page_id = next leaf ──┼──▶          │   next_page_id = INVALID     │
    ├──────────────────────────────┤             ├──────────────────────────────┤
    │ slot directory, in KEY order │             │ slot directory, in KEY order │
    ├──────────────────────────────┤             ├──────────────────────────────┤
    │ entry: key ‖ rid (6 B)       │             │ entry: key ‖ rid ‖ child (4B)│
    │ entry: key ‖ rid             │             │ entry: key ‖ rid ‖ child     │
    │ …                            │             │ …                            │
    └──────────────────────────────┘             └──────────────────────────────┘

**Slot order is key order.**  That is the one invariant this module exists to
maintain, and it is why :meth:`~engine.storage.page.Page.insert_at` had to be
added: the heap's ``insert`` appends wherever there is room, which would leave a
node unsearchable.

Why the record id is part of the key
------------------------------------
A non-unique index has duplicates, and a naive tree stores a *list* of record
ids per key.  Appending the record id to the sort key instead makes every entry
in the tree unique, which buys three things:

* deleting one row's entry is a descent, not a scan of every duplicate;
* a split can happen anywhere, including in the middle of a run of duplicates;
* a page full of one repeated key is still splittable, which the list form is
  not once the list outgrows a page.

PostgreSQL adopted exactly this in version 12 — "make the heap TID a tiebreaker
column" — and reported large reductions in index bloat for low-cardinality
columns.  Internal separators carry the record id for the same reason: without
it, a descent through duplicates could not tell which child to enter.

The comparison, and why the key is not simply concatenated
----------------------------------------------------------
Entries compare as the pair ``(key_bytes, rid_bytes)``, not as the single
concatenated byte string.  Concatenating would reintroduce the prefix problem
:mod:`engine.index.key` avoids: with keys ``"ab"`` and ``"abc"``, the bytes
``"ab" ‖ rid`` and ``"abc" ‖ rid`` interleave, and whether the comparison comes
out right depends on the numeric value of the record id.  Splitting the fixed-
width suffix off by length and comparing a tuple is exact, needs no escaping
layer, and costs one extra ``memcmp`` on the rare occasion the keys are equal.

Capacity
--------
On the default 4 KiB page, an INTEGER leaf entry is 1 (tag) + 8 (value) +
6 (rid) = 15 bytes, plus a 4-byte slot: 19 bytes.  ``(4096 - 24) / 19 ≈ 214``
entries per leaf, so fanout ≈ 214 and three levels address ≈ 9.8 million rows.
That is the whole argument for B+ trees over binary trees: a balanced binary
tree over 9.8 M rows is 24 levels deep and therefore 24 page reads, against 3.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from engine.errors import CorruptPageError, IndexingError
from engine.index.key import MINUS_INFINITY, describe_key
from engine.serialization.types import DataType
from engine.storage.constants import INVALID_PAGE_ID, PageType
from engine.storage.heap import RecordId
from engine.storage.page import PAGE_HEADER_SIZE, SLOT_SIZE, Page

__all__ = [
    "CHILD_SIZE",
    "MINIMUM_ENTRIES_PER_NODE",
    "RID_SIZE",
    "BTreeNode",
    "Entry",
    "decode_rid",
    "encode_rid",
    "minus_infinity_entry",
    "plan_split",
]

#: ``page_id`` as u32 big-endian, ``slot_id`` as u16 big-endian.  Big-endian so
#: the suffix orders the same way its numeric value does, which is what makes it
#: a usable tiebreaker.
_RID: Final = struct.Struct(">IH")
RID_SIZE: Final[int] = _RID.size  # 6

_CHILD: Final = struct.Struct(">I")
CHILD_SIZE: Final[int] = _CHILD.size  # 4

#: A node must hold at least this many entries for splitting to make progress.
#: With two, a split always produces two non-empty halves; with one, a node
#: holding a single oversized entry would split forever.
MINIMUM_ENTRIES_PER_NODE: Final = 2


def encode_rid(record_id: RecordId) -> bytes:
    return _RID.pack(record_id.page_id, record_id.slot_id)


def decode_rid(raw: bytes) -> RecordId:
    return RecordId(*_RID.unpack(raw))


#: The record id attached to a minus-infinity separator.  Page 0 is the meta
#: page and slot 0 of it is never a record, so this address cannot collide with
#: a real one — and it is the numerically smallest, so it sorts first.
_SENTINEL_RID: Final = RecordId(0, 0)


@dataclass(frozen=True, slots=True)
class Entry:
    """One entry in a node.

    ``child_page_id`` is ``INVALID_PAGE_ID`` in a leaf, where ``record_id``
    points into the table's heap, and a real page id in an internal node, where
    ``record_id`` is only part of the separator.
    """

    key: bytes
    record_id: RecordId
    child_page_id: int = INVALID_PAGE_ID

    @property
    def sort_key(self) -> tuple[bytes, bytes]:
        """What ordering actually compares. See the module docstring."""
        return (self.key, encode_rid(self.record_id))

    @property
    def is_minus_infinity(self) -> bool:
        return self.key == MINUS_INFINITY

    def describe(self, data_type: DataType) -> str:
        return describe_key(self.key, data_type)


def minus_infinity_entry(child_page_id: int) -> Entry:
    """The first separator of every internal node."""
    return Entry(MINUS_INFINITY, _SENTINEL_RID, child_page_id)


def plan_split(widths: Sequence[int], capacity: int) -> int:
    """Choose where to cut an overfull node so both halves fit.

    ``widths`` is the byte cost of each entry including its slot; ``capacity``
    is what one page can hold.  Returns the index of the first entry that moves
    to the right-hand node, always in ``[1, len(widths) - 1]`` so neither side
    comes out empty.

    Balancing by **bytes**, not by entry count, matters as soon as entries vary
    in width — a TEXT index holding ``"a"`` and a 200-character string would
    otherwise cut 50/50 by count and leave one page nearly full, which splits
    again on the very next insert.

    The byte-balanced point is then clamped to the range where both halves
    actually fit.  That range is never empty: a node only overflows once, so the
    total is at most ``capacity`` (what already fitted) plus one entry (at most
    ``capacity``), and any single entry fits on a page by itself.
    """
    count = len(widths)
    if count < MINIMUM_ENTRIES_PER_NODE:
        raise IndexingError(f"cannot split a node holding {count} entries")

    # Largest prefix that still fits, and smallest prefix whose suffix fits.
    running = 0
    max_left = 0
    for index, width in enumerate(widths):
        running += width
        if running > capacity:
            break
        max_left = index + 1
    running = 0
    min_left = count
    for index in reversed(range(count)):
        running += widths[index]
        if running > capacity:
            break
        min_left = index

    low = max(min_left, 1)
    high = min(max_left, count - 1)
    if low > high:  # pragma: no cover - impossible; see the docstring
        raise IndexingError(
            f"cannot split {count} entries totalling {sum(widths)} bytes into two "
            f"{capacity}-byte pages"
        )

    total = sum(widths)
    running = 0
    balanced = count // 2
    for index, width in enumerate(widths):
        running += width
        if running * 2 >= total:
            balanced = index + 1
            break
    return min(max(balanced, low), high)


class BTreeNode:
    """A view over one page, interpreting its slots as index entries.

    Holds no state of its own beyond the page, so two nodes wrapping the same
    page cannot disagree.  Mutating methods write into the page buffer; the
    caller is responsible for handing the page back to the pager.
    """

    __slots__ = ("page",)

    def __init__(self, page: Page) -> None:
        if page.page_type not in (PageType.BTREE_LEAF, PageType.BTREE_INTERNAL):
            raise CorruptPageError(
                f"page {page.page_id} is a {page.page_type.name} page, not a B+ tree node"
            )
        self.page = page

    # -- identity ----------------------------------------------------------

    @property
    def page_id(self) -> int:
        return self.page.page_id

    @property
    def is_leaf(self) -> bool:
        return self.page.page_type is PageType.BTREE_LEAF

    @property
    def count(self) -> int:
        return self.page.slot_count

    @property
    def next_leaf_id(self) -> int:
        """The next leaf in key order, or ``INVALID_PAGE_ID`` at the end.

        Stored in the page header's ``next_page_id``, the same field a heap uses
        for its page chain.  Safe to share: a page belongs to exactly one
        structure, and internal nodes never set it — a range scan only ever
        walks leaves, so no internal sibling link is needed.
        """
        if not self.is_leaf:
            return INVALID_PAGE_ID
        return self.page.next_page_id

    @next_leaf_id.setter
    def next_leaf_id(self, value: int) -> None:
        if not self.is_leaf:
            raise IndexingError("only leaves have a sibling link")
        self.page.next_page_id = value

    # -- reading -----------------------------------------------------------

    def entry_at(self, index: int) -> Entry:
        payload = self.page.read(index)
        if payload is None:
            raise CorruptPageError(
                f"page {self.page_id}: slot {index} is dead, but B+ tree nodes "
                f"have no tombstones; delete_at removes entries outright"
            )
        return self._decode(payload)

    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._decode(payload) for _, payload in self.page.iter_records())

    def iter_entries(self) -> Iterator[Entry]:
        for _, payload in self.page.iter_records():
            yield self._decode(payload)

    def _decode(self, payload: bytes) -> Entry:
        suffix = RID_SIZE if self.is_leaf else RID_SIZE + CHILD_SIZE
        if len(payload) < suffix + 1:
            raise CorruptPageError(
                f"page {self.page_id}: entry is {len(payload)} bytes, too short "
                f"for a {'leaf' if self.is_leaf else 'internal'} entry"
            )
        if self.is_leaf:
            return Entry(payload[:-RID_SIZE], decode_rid(payload[-RID_SIZE:]))
        return Entry(
            payload[:-suffix],
            decode_rid(payload[-suffix:-CHILD_SIZE]),
            _CHILD.unpack(payload[-CHILD_SIZE:])[0],
        )

    def _encode(self, entry: Entry) -> bytes:
        if self.is_leaf:
            return entry.key + encode_rid(entry.record_id)
        if entry.child_page_id == INVALID_PAGE_ID:
            raise IndexingError("an internal entry must point at a child page")
        return entry.key + encode_rid(entry.record_id) + _CHILD.pack(entry.child_page_id)

    # -- searching ---------------------------------------------------------
    #
    # Both searches are binary, O(log entries) comparisons per node.  A linear
    # scan would be O(entries) and, at a fanout of 214, roughly 27x more
    # comparisons per level — which is the difference between a B+ tree being
    # worth building and not.

    def lower_bound(self, key: bytes, record_id: RecordId | None = None) -> int:
        """Index of the first entry whose sort key is ``>= (key, record_id)``.

        With ``record_id=None`` the bound is the first entry with *any* record
        id for ``key`` — which is what a search for "all rows with this key"
        needs, and why the parameter is optional rather than defaulting to the
        minimum record id.
        """
        target = (key, encode_rid(record_id) if record_id is not None else b"")
        low, high = 0, self.count
        while low < high:
            middle = (low + high) // 2
            if self.entry_at(middle).sort_key < target:
                low = middle + 1
            else:
                high = middle
        return low

    def upper_bound(self, key: bytes) -> int:
        """Index of the first entry whose key is strictly greater than ``key``."""
        low, high = 0, self.count
        while low < high:
            middle = (low + high) // 2
            if self.entry_at(middle).key <= key:
                low = middle + 1
            else:
                high = middle
        return low

    def child_index_for(self, key: bytes, record_id: RecordId | None = None) -> int:
        """Index of the separator whose subtree may contain ``(key, record_id)``.

        The last entry whose separator is ``<=`` the target.  Slot 0's separator
        is minus infinity, so this always finds one — the reason internal nodes
        carry that sentinel instead of a special "leftmost child" header field.
        """
        if not self.count:
            raise CorruptPageError(f"page {self.page_id}: internal node has no children")
        bound = self.lower_bound(key, record_id)
        if bound < self.count and self.entry_at(bound).sort_key == (
            key,
            encode_rid(record_id) if record_id is not None else b"",
        ):
            return bound
        return max(bound - 1, 0)

    def child_for(self, key: bytes, record_id: RecordId | None = None) -> int:
        return self.entry_at(self.child_index_for(key, record_id)).child_page_id

    # -- writing -----------------------------------------------------------

    def insert_entry(self, entry: Entry) -> bool:
        """Insert into key order. ``False`` means the node is full and must split."""
        return self.insert_entry_at(self.lower_bound(entry.key, entry.record_id), entry)

    def insert_entry_at(self, index: int, entry: Entry) -> bool:
        payload = self._encode(entry)
        if self.page.insert_at(index, payload):
            return True
        # Deletions leave payload bytes stranded until something compacts, so try
        # that before declaring the node full — one compaction is far cheaper
        # than a split, which costs a page allocation and touches the parent.
        if self.page.reclaimable_space > 0:
            self.page.compact()
            return self.page.insert_at(index, payload)
        return False

    def delete_entry_at(self, index: int) -> bool:
        return self.page.delete_at(index)

    def remove(self, key: bytes, record_id: RecordId) -> bool:
        index = self.lower_bound(key, record_id)
        if index >= self.count:
            return False
        entry = self.entry_at(index)
        if entry.key != key or entry.record_id != record_id:
            return False
        return self.delete_entry_at(index)

    def replace_all(self, entries: list[Entry]) -> None:
        """Rewrite the node to hold exactly ``entries``, in the order given.

        Used by both halves of a split. Raises rather than silently dropping if
        the entries do not fit — the split point is chosen by byte size for
        precisely this reason, so a failure here is a bug, not a full node.
        """
        self.page.clear_records()
        for position, entry in enumerate(entries):
            if not self.page.insert_at(position, self._encode(entry)):
                raise IndexingError(
                    f"page {self.page_id}: {len(entries)} entries do not fit after a "
                    f"split (failed at {position})"
                )

    # -- splitting ---------------------------------------------------------

    @property
    def usable_bytes(self) -> int:
        """Bytes a full node can hold, counting slot directory entries."""
        return self.page.page_size - PAGE_HEADER_SIZE

    def entry_widths(self, entries: Sequence[Entry]) -> list[int]:
        """Total cost of each entry: payload plus its slot directory entry."""
        return [len(self._encode(entry)) + SLOT_SIZE for entry in entries]

    def split_index(self, entries: Sequence[Entry]) -> int:
        return plan_split(self.entry_widths(entries), self.usable_bytes)

    @property
    def free_space(self) -> int:
        return self.page.free_space

    def __repr__(self) -> str:
        kind = "leaf" if self.is_leaf else "internal"
        return (
            f"<BTreeNode {kind} page={self.page_id} entries={self.count} "
            f"free={self.free_space}B>"
        )
