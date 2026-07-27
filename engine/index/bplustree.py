"""The B+ tree: turning O(pages) into O(log pages).

A sequential scan of a million-row table reads every page to find one row.  A
B+ tree finds it in the height of the tree — three page reads at ChenDB's
fanout.  That single change is what makes a database a database rather than a
file of rows.

Shape
-----
::

                          ┌──────────────────────────┐
       internal           │  -∞ │ 40 │ 80            │   root, page 7
       (separators only)  └───┬──┴──┬─┴──┬───────────┘
                    ┌─────────┘     │    └─────────┐
                    ▼               ▼              ▼
              ┌──────────┐    ┌──────────┐   ┌──────────┐
       leaves │ 10 20 30 │───▶│ 40 55 70 │──▶│ 80 90 99 │──▶ ∅
              └──────────┘    └──────────┘   └──────────┘
               each entry: key ‖ record id → a row in the heap

Three properties are doing all the work.

**Every value lives in a leaf.**  Internal nodes hold separators only, so they
are pure routing and stay small — which is what makes the fanout large and the
tree short.  A plain B-tree stores values in internal nodes too, so its internal
nodes hold fewer keys and it is taller for the same data.

**Leaves are linked.**  A range scan descends once to find the low bound, then
walks sideways.  ``WHERE age BETWEEN 30 AND 40`` costs one descent plus the
leaves the range actually touches, with no re-descent per row.

**Growth is at the root.**  A node that overflows splits and pushes a separator
up; when the *root* splits, a new root is allocated above it and the tree gets
one level taller.  Every leaf is therefore always at the same depth, with no
rebalancing pass — the property that makes worst-case and average-case lookup
the same cost.

Complexity
----------
======================  ==========================================
Operation               Cost, *n* rows, fanout *f*
======================  ==========================================
``search``              O(log_f n) page reads
``insert``              O(log_f n) reads; a split adds O(1) writes
``delete``              O(log_f n) reads, one write
``range_scan``          O(log_f n) + O(matching leaves)
build from *n* rows     O(n log_f n) — see the note on bulk loading
======================  ==========================================

At *f* = 214 and *n* = 10⁶: ``log_f n`` ≈ 2.6, so three page reads, against
roughly 4400 pages for a sequential scan.

What this implementation does not do
------------------------------------
**No merging on delete.**  An entry is removed from its leaf; the leaf is left
underfull, and an emptied leaf is left in the tree rather than unlinked.  This
is a deliberate choice, not an omission: merging requires locking a node's
sibling *and* its parent while a concurrent descent may be passing through, and
getting that wrong corrupts the tree in ways that only show up under load.
PostgreSQL took until version 11 to reclaim empty index pages, and still never
merges partly-full ones; SQLite does merge, and pays for it with a much more
complex balance routine.  The cost here is that a delete-heavy index keeps its
pages: space is reused by later inserts into the same key range, but never
returned to the file.

**No bulk loading.**  Building an index over an existing table inserts row by
row, at O(n log n) with a split every *f*/2 rows.  A real system sorts the keys
first and packs leaves to capacity in one pass — O(n log n) for the sort, then
O(n) — which also produces a tree with no wasted space.  ``CREATE INDEX`` on a
large table would notice.

**No concurrency.**  One writer at a time, enforced by the database-level lock.
Real B+ trees use latch coupling (grab the child's latch, release the parent's)
and the Blink-tree trick of a right-link that lets a reader recover when a
concurrent split moves the key it wanted.  Milestone 10 is where that lands.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

from engine.diagnostics.events import (
    IndexDescentEvent,
    IndexSearchEvent,
    NodeSplitEvent,
    RangeScanEvent,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import CorruptPageError, IndexingError, UniqueViolation
from engine.index.key import describe_key, is_null_key
from engine.index.node import BTreeNode, Entry, encode_rid, minus_infinity_entry
from engine.serialization.types import DataType
from engine.storage.constants import INVALID_PAGE_ID, PageType
from engine.storage.heap import RecordId
from engine.storage.pager import Pager

__all__ = ["BPlusTree", "IndexStats", "NodeSnapshot", "TreeSnapshot"]

#: A tree deeper than this is a corrupt-page cycle, not a real tree: at the
#: smallest supported page size the fanout is still ~12, so 32 levels would
#: address more rows than there are atoms worth indexing.
_MAX_DEPTH: Final = 32


@dataclass(slots=True)
class IndexStats:
    """What the index has done since it was opened."""

    searches: int = 0
    inserts: int = 0
    deletes: int = 0
    splits: int = 0
    root_splits: int = 0
    range_scans: int = 0
    nodes_visited: int = 0
    leaves_visited: int = 0
    pages_allocated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "searches": self.searches,
            "inserts": self.inserts,
            "deletes": self.deletes,
            "splits": self.splits,
            "root_splits": self.root_splits,
            "range_scans": self.range_scans,
            "nodes_visited": self.nodes_visited,
            "leaves_visited": self.leaves_visited,
            "pages_allocated": self.pages_allocated,
        }


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """One node, decoded for display.

    Frozen and self-contained so the API can serialise it without holding any
    engine lock — the same reason :mod:`engine.storage.inspect` returns
    dataclasses rather than live pages.
    """

    page_id: int
    level: int
    """0 at the leaves, increasing toward the root — so sibling leaves match."""
    is_leaf: bool
    keys: tuple[str, ...]
    """Rendered separators or keys, in slot order."""
    children: tuple[int, ...]
    """Child page ids, empty for a leaf."""
    record_ids: tuple[str, ...]
    """``(page,slot)`` per entry, empty for an internal node."""
    next_leaf_id: int | None
    free_bytes: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """The whole tree as a flat node list plus a root id.

    Flat rather than nested, for the same reason the AST and the plan are:
    a recursive JSON shape forces the client to walk it to find anything, and
    makes a cycle in a corrupt tree an infinite response instead of a visible
    duplicate.
    """

    root_page_id: int
    height: int
    nodes: tuple[NodeSnapshot, ...]
    truncated: bool = False
    """True when the node budget was hit, so the client can say so."""


class BPlusTree:
    """A disk-backed B+ tree over one encoded column.

    The tree is *typed only for display*: comparisons work on the encoded byte
    strings from :mod:`engine.index.key`, so nothing here branches on
    ``data_type``.  It is carried purely so events and snapshots can render keys
    as values rather than hex.
    """

    __slots__ = (
        "_data_type",
        "_name",
        "_on_root_changed",
        "_pager",
        "_root_page_id",
        "_stats",
        "_tracer",
        "_unique",
    )

    def __init__(
        self,
        pager: Pager,
        root_page_id: int,
        *,
        name: str = "index",
        data_type: DataType = DataType.INTEGER,
        unique: bool = False,
        on_root_changed: Callable[[int], None] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._pager = pager
        self._root_page_id = root_page_id
        self._name = name
        self._data_type = data_type
        self._unique = unique
        self._on_root_changed = on_root_changed
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._stats = IndexStats()

    @classmethod
    def create(
        cls,
        pager: Pager,
        *,
        name: str = "index",
        data_type: DataType = DataType.INTEGER,
        unique: bool = False,
        on_root_changed: Callable[[int], None] | None = None,
        tracer: Tracer | None = None,
    ) -> BPlusTree:
        """Build an empty tree: a single leaf, which is also the root.

        A tree of height 1 with no entries. It stays that way until the first
        split, so a small table's index costs exactly one page.
        """
        page = pager.allocate_page(PageType.BTREE_LEAF)
        pager.write_page(page)
        tree = cls(
            pager,
            page.page_id,
            name=name,
            data_type=data_type,
            unique=unique,
            on_root_changed=on_root_changed,
            tracer=tracer,
        )
        tree._stats.pages_allocated += 1
        if on_root_changed is not None:
            on_root_changed(page.page_id)
        return tree

    # -- properties --------------------------------------------------------

    @property
    def root_page_id(self) -> int:
        return self._root_page_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def data_type(self) -> DataType:
        return self._data_type

    @property
    def unique(self) -> bool:
        return self._unique

    @property
    def stats(self) -> IndexStats:
        return self._stats

    # -- node access -------------------------------------------------------

    def _node(self, page_id: int) -> BTreeNode:
        self._stats.nodes_visited += 1
        return BTreeNode(self._pager.read_page(page_id))

    def _write(self, node: BTreeNode) -> None:
        self._pager.write_page(node.page)

    # -- searching ---------------------------------------------------------

    def _descend(
        self, key: bytes, record_id: RecordId | None
    ) -> tuple[BTreeNode, list[tuple[int, int]]]:
        """Walk from the root to the leaf that would hold ``(key, record_id)``.

        Returns the leaf and the path taken as ``(page_id, child_index)`` pairs,
        innermost last.  The path is what makes a bottom-up split possible
        without parent pointers on disk — storing parents on the page would mean
        rewriting every child of a node that moves, and would have to be kept
        correct under splits, which is a large amount of work for something a
        16-entry Python list gives away.
        """
        path: list[tuple[int, int]] = []
        node = self._node(self._root_page_id)
        depth = 0
        while not node.is_leaf:
            depth += 1
            if depth > _MAX_DEPTH:
                raise CorruptPageError(
                    f"index {self._name!r}: descent exceeded {_MAX_DEPTH} levels, "
                    f"the page chain is cyclic"
                )
            index = node.child_index_for(key, record_id)
            child_page_id = node.entry_at(index).child_page_id
            if self._tracer.verbose:
                self._tracer.emit(
                    IndexDescentEvent(
                        index_name=self._name,
                        page_id=node.page_id,
                        tree_level=depth - 1,
                        child_page_id=child_page_id,
                        separator=describe_key(node.entry_at(index).key, self._data_type),
                    )
                )
            path.append((node.page_id, index))
            node = self._node(child_page_id)
        return node, path

    def search(self, key: bytes) -> list[RecordId]:
        """Every record id stored under ``key``, in tree order.

        Duplicates may straddle a leaf boundary — the record-id tiebreaker means
        a split can fall inside a run of equal keys — so this follows the
        sibling links until it sees a larger key.
        """
        started = time.perf_counter_ns()
        self._stats.searches += 1
        pages_before = self._stats.nodes_visited

        # Descending with no record id lands on the *first* leaf that could hold
        # the key. When the key exactly equals a separator, that is the leaf to
        # its left, which may hold no match at all — so this always has to be
        # willing to step right. PostgreSQL calls the same manoeuvre "moving
        # right", and needs it for a stronger reason: a concurrent split may
        # have moved the key after the descent read the parent.
        leaf, path = self._descend(key, None)
        found: list[RecordId] = []
        index = leaf.lower_bound(key)
        while True:
            exhausted = True
            while index < leaf.count:
                entry = leaf.entry_at(index)
                if entry.key != key:
                    exhausted = False
                    break
                found.append(entry.record_id)
                index += 1
            if not exhausted or leaf.next_leaf_id == INVALID_PAGE_ID:
                break
            leaf = self._node(leaf.next_leaf_id)
            index = 0

        if self._tracer.storage:
            self._tracer.emit(
                IndexSearchEvent(
                    index_name=self._name,
                    key=describe_key(key, self._data_type),
                    found=bool(found),
                    matches=len(found),
                    pages_visited=self._stats.nodes_visited - pages_before,
                    depth=len(path) + 1,
                    duration_ns=time.perf_counter_ns() - started,
                )
            )
        return found

    def descent_path(self, key: bytes) -> list[int]:
        """Page ids from the root to the leaf a search for ``key`` reaches.

        Exposed for the tree view, which highlights the path a lookup takes.
        Recomputes the descent rather than recording it during
        :meth:`search`, so the search path stays free of display concerns.
        """
        leaf, path = self._descend(key, None)
        return [page_id for page_id, _ in path] + [leaf.page_id]

    def search_one(self, key: bytes) -> RecordId | None:
        """The first record id under ``key``. For a unique index, the only one."""
        matches = self.search(key)
        return matches[0] if matches else None

    def contains(self, key: bytes) -> bool:
        return bool(self.search(key))

    def range_scan(
        self,
        low: bytes | None = None,
        high: bytes | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> Iterator[tuple[bytes, RecordId]]:
        """Yield ``(key, record_id)`` in key order between the bounds.

        ``None`` means unbounded on that side, so ``range_scan()`` is a full
        ordered scan of the index — which is how ``ORDER BY`` on an indexed
        column avoids a sort.

        A generator: the caller pulls one entry at a time, so ``LIMIT 10`` over
        a matching range of a million reads only the leaves it needs. That is
        the same contract :class:`~engine.storage.heap.HeapFile.scan` offers,
        and what lets the volcano executor stay streaming end to end.
        """
        started = time.perf_counter_ns()
        self._stats.range_scans += 1
        leaves = 0
        emitted = 0

        if low is None:
            leaf = self._leftmost_leaf()
            index = 0
        else:
            leaf, _ = self._descend(low, None)
            index = leaf.lower_bound(low)

        while True:
            leaves += 1
            self._stats.leaves_visited += 1
            while index < leaf.count:
                entry = leaf.entry_at(index)
                if low is not None and not include_low and entry.key == low:
                    index += 1
                    continue
                if high is not None and (
                    entry.key > high or (not include_high and entry.key == high)
                ):
                    self._emit_range(low, high, leaves, emitted, started)
                    return
                emitted += 1
                yield entry.key, entry.record_id
                index += 1
            next_id = leaf.next_leaf_id
            if next_id == INVALID_PAGE_ID:
                break
            leaf = self._node(next_id)
            index = 0

        self._emit_range(low, high, leaves, emitted, started)

    def _emit_range(
        self,
        low: bytes | None,
        high: bytes | None,
        leaves: int,
        emitted: int,
        started: int,
    ) -> None:
        if not self._tracer.storage:
            return
        self._tracer.emit(
            RangeScanEvent(
                index_name=self._name,
                low=describe_key(low, self._data_type) if low is not None else "",
                high=describe_key(high, self._data_type) if high is not None else "",
                leaves_visited=leaves,
                rows_emitted=emitted,
                duration_ns=time.perf_counter_ns() - started,
            )
        )

    def _leftmost_leaf(self) -> BTreeNode:
        node = self._node(self._root_page_id)
        depth = 0
        while not node.is_leaf:
            depth += 1
            if depth > _MAX_DEPTH:
                raise CorruptPageError(f"index {self._name!r}: cyclic page chain")
            node = self._node(node.entry_at(0).child_page_id)
        return node

    # -- inserting ---------------------------------------------------------

    def insert(self, key: bytes, record_id: RecordId) -> None:
        """Add one entry, splitting nodes upward as needed.

        For a unique index, a duplicate non-NULL key raises
        :class:`~engine.errors.UniqueViolation`.  NULLs are exempt: SQL treats
        two unknowns as not known to be equal, so ``UNIQUE`` permits any number
        of them — the behaviour of PostgreSQL, SQLite and the standard alike.
        """
        if self._unique and not is_null_key(key) and self.contains(key):
            raise UniqueViolation(
                f"index {self._name!r} is unique and already contains "
                f"{describe_key(key, self._data_type)}"
            )

        leaf, path = self._descend(key, record_id)
        entry = Entry(key, record_id)
        self._stats.inserts += 1

        if leaf.insert_entry(entry):
            self._write(leaf)
            return

        self._split_leaf(leaf, path, entry)

    def _split_leaf(
        self, leaf: BTreeNode, path: list[tuple[int, int]], entry: Entry
    ) -> None:
        """Split a full leaf, then push a separator into the parent."""
        entries = list(leaf.entries())
        entries.insert(leaf.lower_bound(entry.key, entry.record_id), entry)
        cut = leaf.split_index(entries)
        left, right = entries[:cut], entries[cut:]

        sibling_page = self._pager.allocate_page(PageType.BTREE_LEAF)
        self._stats.pages_allocated += 1
        sibling = BTreeNode(sibling_page)
        sibling.replace_all(right)
        # Splice into the leaf chain before rewriting the left half, so the
        # chain is never observably broken even if a write fails in between.
        sibling.next_leaf_id = leaf.next_leaf_id
        leaf.replace_all(left)
        leaf.next_leaf_id = sibling.page_id
        self._write(sibling)
        self._write(leaf)

        self._stats.splits += 1
        # The separator is a *copy* of the right half's first key. Copied, not
        # moved: the key is still a real value that lives in a leaf, because in a
        # B+ tree every value is in a leaf. Internal splits move instead.
        separator = Entry(right[0].key, right[0].record_id, sibling.page_id)
        self._emit_split(leaf.page_id, sibling.page_id, 0, separator, is_root=not path)
        self._insert_into_parent(path, separator, leaf.page_id)

    def _insert_into_parent(
        self, path: list[tuple[int, int]], separator: Entry, left_page_id: int
    ) -> None:
        """Place ``separator`` in the parent, splitting upward if it is full."""
        if not path:
            self._grow_root(left_page_id, separator)
            return

        parent_page_id, _ = path[-1]
        parent = self._node(parent_page_id)
        if parent.insert_entry(separator):
            self._write(parent)
            return

        entries = list(parent.entries())
        entries.insert(parent.lower_bound(separator.key, separator.record_id), separator)
        cut = parent.split_index(entries)

        # An internal split *moves* the middle separator up rather than copying
        # it. It is pure routing information, not a value, so leaving a copy
        # behind would waste a slot and, worse, make the same separator appear
        # at two levels — which is legal but confusing to read in the tree view.
        promoted = entries[cut]
        left = entries[:cut]
        right = [minus_infinity_entry(promoted.child_page_id), *entries[cut + 1 :]]

        sibling_page = self._pager.allocate_page(PageType.BTREE_INTERNAL)
        self._stats.pages_allocated += 1
        sibling = BTreeNode(sibling_page)
        sibling.replace_all(right)
        parent.replace_all(left)
        self._write(sibling)
        self._write(parent)

        self._stats.splits += 1
        level = len(path)
        promoted_up = Entry(promoted.key, promoted.record_id, sibling.page_id)
        self._emit_split(
            parent.page_id, sibling.page_id, level, promoted_up, is_root=len(path) == 1
        )
        self._insert_into_parent(path[:-1], promoted_up, parent.page_id)

    def _grow_root(self, left_page_id: int, separator: Entry) -> None:
        """Add a level. The only operation that changes the tree's height.

        The old root keeps its page id and becomes the leftmost child, so no
        existing pointer has to be rewritten.
        """
        page = self._pager.allocate_page(PageType.BTREE_INTERNAL)
        self._stats.pages_allocated += 1
        root = BTreeNode(page)
        root.replace_all([minus_infinity_entry(left_page_id), separator])
        self._write(root)

        self._root_page_id = root.page_id
        self._stats.root_splits += 1
        if self._on_root_changed is not None:
            self._on_root_changed(root.page_id)

    def _emit_split(
        self,
        page_id: int,
        new_page_id: int,
        tree_level: int,
        separator: Entry,
        *,
        is_root: bool,
    ) -> None:
        if not self._tracer.storage:
            return
        self._tracer.emit(
            NodeSplitEvent(
                index_name=self._name,
                page_id=page_id,
                new_page_id=new_page_id,
                tree_level=tree_level,
                promoted_key=describe_key(separator.key, self._data_type),
                is_root_split=is_root,
            )
        )

    # -- deleting ----------------------------------------------------------

    def delete(self, key: bytes, record_id: RecordId) -> bool:
        """Remove one entry. ``False`` if it was not there.

        Leaves the node underfull rather than merging — see the module
        docstring for why that is a choice and not a shortcut.
        """
        leaf, _ = self._descend(key, record_id)
        if not leaf.remove(key, record_id):
            return False
        self._write(leaf)
        self._stats.deletes += 1
        return True

    def delete_key(self, key: bytes) -> int:
        """Remove every entry for ``key``; returns how many went."""
        removed = 0
        for record_id in self.search(key):
            if self.delete(key, record_id):
                removed += 1
        return removed

    # -- introspection -----------------------------------------------------

    @property
    def height(self) -> int:
        """Levels from root to leaf inclusive. A fresh tree has height 1."""
        node = self._node(self._root_page_id)
        height = 1
        while not node.is_leaf:
            if height > _MAX_DEPTH:
                raise CorruptPageError(f"index {self._name!r}: cyclic page chain")
            node = self._node(node.entry_at(0).child_page_id)
            height += 1
        return height

    def count(self) -> int:
        """Entries in the tree. O(leaves) — nothing caches a count."""
        return sum(1 for _ in self.range_scan())

    def page_ids(self) -> list[int]:
        """Every page the tree occupies, root first, then level by level."""
        return [node.page_id for node in self._walk()]

    def _walk(self) -> Iterator[BTreeNode]:
        """Breadth-first, so nodes come out grouped by level."""
        frontier = [self._root_page_id]
        seen: set[int] = set()
        depth = 0
        while frontier:
            depth += 1
            if depth > _MAX_DEPTH:
                raise CorruptPageError(f"index {self._name!r}: cyclic page chain")
            next_frontier: list[int] = []
            for page_id in frontier:
                if page_id in seen:
                    raise CorruptPageError(
                        f"index {self._name!r}: page {page_id} appears twice in the tree"
                    )
                seen.add(page_id)
                node = self._node(page_id)
                yield node
                if not node.is_leaf:
                    next_frontier.extend(
                        entry.child_page_id for entry in node.iter_entries()
                    )
            frontier = next_frontier

    def snapshot(self, *, max_nodes: int = 512) -> TreeSnapshot:
        """Decode the whole tree for display, bounded by ``max_nodes``.

        Bounded because a tree over a large table has thousands of leaves and no
        browser wants them all; the client is told when it was cut short rather
        than being handed a silently partial tree.  Levels are numbered from the
        leaves up, so sibling leaves share a level even in a tree that is
        temporarily uneven mid-split.
        """
        height = self.height
        nodes: list[NodeSnapshot] = []
        truncated = False
        depth_of: dict[int, int] = {self._root_page_id: 0}

        for node in self._walk():
            if len(nodes) >= max_nodes:
                truncated = True
                break
            depth = depth_of.get(node.page_id, 0)
            entries = node.entries()
            if not node.is_leaf:
                for entry in entries:
                    depth_of[entry.child_page_id] = depth + 1
            nodes.append(
                NodeSnapshot(
                    page_id=node.page_id,
                    level=height - 1 - depth,
                    is_leaf=node.is_leaf,
                    keys=tuple(
                        describe_key(entry.key, self._data_type) for entry in entries
                    ),
                    children=()
                    if node.is_leaf
                    else tuple(entry.child_page_id for entry in entries),
                    record_ids=tuple(str(entry.record_id) for entry in entries)
                    if node.is_leaf
                    else (),
                    next_leaf_id=None
                    if not node.is_leaf or node.next_leaf_id == INVALID_PAGE_ID
                    else node.next_leaf_id,
                    free_bytes=node.free_space,
                    entry_count=len(entries),
                )
            )

        return TreeSnapshot(
            root_page_id=self._root_page_id,
            height=height,
            nodes=tuple(nodes),
            truncated=truncated,
        )

    def verify(self) -> None:
        """Assert every structural invariant. For tests and the CLI, not the API.

        Checks that keys are sorted within each node, that every separator
        actually bounds its subtree, that all leaves are at the same depth, and
        that the sibling chain visits every leaf exactly once in key order. A
        tree can be subtly wrong in ways no single operation notices — a split
        that promoted the wrong key still answers most queries correctly — so
        the tests assert the whole shape rather than sampled lookups.
        """
        leaf_depths: set[int] = set()
        self._verify_subtree(self._root_page_id, None, None, 0, leaf_depths)
        if len(leaf_depths) > 1:
            raise IndexingError(
                f"index {self._name!r} is unbalanced: leaves at depths {sorted(leaf_depths)}"
            )

        previous: tuple[bytes, bytes] | None = None
        for key, record_id in self.range_scan():
            current = (key, encode_rid(record_id))
            if previous is not None and current <= previous:
                raise IndexingError(
                    f"index {self._name!r}: leaf chain is out of order or has a "
                    f"duplicate entry at {describe_key(key, self._data_type)}"
                )
            previous = current

    def _verify_subtree(
        self,
        page_id: int,
        low: bytes | None,
        high: bytes | None,
        depth: int,
        leaf_depths: set[int],
    ) -> None:
        node = BTreeNode(self._pager.read_page(page_id))
        entries = node.entries()

        for earlier, later in itertools.pairwise(entries):
            if earlier.sort_key >= later.sort_key:
                raise IndexingError(
                    f"index {self._name!r}: page {page_id} entries are not sorted"
                )
        for entry in entries:
            if entry.is_minus_infinity:
                continue
            if low is not None and entry.key < low:
                raise IndexingError(
                    f"index {self._name!r}: page {page_id} holds a key below its "
                    f"parent separator"
                )
            if high is not None and entry.key > high:
                raise IndexingError(
                    f"index {self._name!r}: page {page_id} holds a key above its "
                    f"parent separator"
                )

        if node.is_leaf:
            leaf_depths.add(depth)
            return
        if not entries:
            raise IndexingError(
                f"index {self._name!r}: internal page {page_id} has no children"
            )
        if not entries[0].is_minus_infinity:
            raise IndexingError(
                f"index {self._name!r}: internal page {page_id} does not start at -∞"
            )
        for position, entry in enumerate(entries):
            child_low = None if entry.is_minus_infinity else entry.key
            child_high = entries[position + 1].key if position + 1 < len(entries) else high
            self._verify_subtree(
                entry.child_page_id, child_low, child_high, depth + 1, leaf_depths
            )

    def __repr__(self) -> str:
        return (
            f"<BPlusTree {self._name!r} root={self._root_page_id} "
            f"unique={self._unique} {self._stats}>"
        )
