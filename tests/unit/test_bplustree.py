"""The B+ tree.

Every test that mutates the tree finishes by calling :meth:`BPlusTree.verify`,
which asserts the *whole* structure: sorted within each node, every separator
bounding its subtree, all leaves at one depth, and the sibling chain visiting
every entry in order.  That matters because a B+ tree can be subtly broken and
still answer most queries — a split that promotes the wrong key loses only the
rows that happen to fall in the gap — so sampled lookups are not enough evidence.

Pages are deliberately tiny (256 bytes, the format's minimum) so a handful of
keys forces a split.  At the 4 KiB default it would take hundreds of rows to
reach depth 2 and thousands to reach depth 3, which would make these tests slow
without testing anything different.
"""

from __future__ import annotations

import random

import pytest

from engine.errors import CorruptPageError, IndexingError, UniqueViolation
from engine.index.bplustree import BPlusTree
from engine.index.key import decode_key, encode_key
from engine.index.node import BTreeNode, Entry, plan_split
from engine.serialization.types import DataType
from engine.storage.constants import INVALID_PAGE_ID, PageType
from engine.storage.heap import RecordId
from engine.storage.pager import Pager

TINY_PAGE_SIZE = 256


@pytest.fixture
def pager(tmp_path) -> Pager:
    handle = Pager(tmp_path / "index.chendb", page_size=TINY_PAGE_SIZE, create=True)
    yield handle
    handle.close()


@pytest.fixture
def tree(pager: Pager) -> BPlusTree:
    return BPlusTree.create(pager, name="ix", data_type=DataType.INTEGER)


def key(value, data_type: DataType = DataType.INTEGER) -> bytes:
    return encode_key(value, data_type)


def rid(n: int) -> RecordId:
    """A distinct record id per n, spread over pages so ids are not all equal."""
    return RecordId(1 + n // 16, n % 16)


def fill(tree: BPlusTree, values, data_type: DataType = DataType.INTEGER) -> None:
    for position, value in enumerate(values):
        tree.insert(encode_key(value, data_type), rid(position))


def scanned(tree: BPlusTree, data_type: DataType = DataType.INTEGER) -> list:
    return [decode_key(k, data_type) for k, _ in tree.range_scan()]


# -- an empty tree ----------------------------------------------------------


def test_a_new_tree_is_one_empty_leaf(tree: BPlusTree, pager: Pager):
    assert tree.height == 1
    assert tree.count() == 0
    assert tree.page_ids() == [tree.root_page_id]
    assert pager.read_page(tree.root_page_id).page_type is PageType.BTREE_LEAF
    tree.verify()


def test_searching_an_empty_tree_finds_nothing(tree: BPlusTree):
    assert tree.search(key(1)) == []
    assert tree.search_one(key(1)) is None
    assert not tree.contains(key(1))
    assert list(tree.range_scan()) == []


def test_deleting_from_an_empty_tree_returns_false(tree: BPlusTree):
    assert tree.delete(key(1), rid(0)) is False


# -- insert and search ------------------------------------------------------


def test_a_single_entry_is_found(tree: BPlusTree):
    tree.insert(key(42), RecordId(9, 3))
    assert tree.search(key(42)) == [RecordId(9, 3)]
    assert tree.search_one(key(42)) == RecordId(9, 3)
    assert tree.search(key(41)) == []
    tree.verify()


def test_entries_come_back_sorted_whatever_order_they_went_in(tree: BPlusTree):
    values = list(range(200))
    shuffled = values.copy()
    random.Random(11).shuffle(shuffled)
    fill(tree, shuffled)
    tree.verify()
    assert scanned(tree) == values


def test_negative_keys_interleave_correctly(tree: BPlusTree):
    values = list(range(-100, 100))
    random.Random(2).shuffle(values)
    fill(tree, values)
    tree.verify()
    assert scanned(tree) == sorted(values)


def test_every_inserted_key_is_findable(tree: BPlusTree):
    values = list(range(300))
    random.Random(5).shuffle(values)
    fill(tree, values)
    for position, value in enumerate(values):
        assert tree.search(key(value)) == [rid(position)], value


# -- splitting --------------------------------------------------------------


def test_the_root_splits_and_the_tree_grows_a_level(tree: BPlusTree, pager: Pager):
    before = tree.height
    old_root = tree.root_page_id
    fill(tree, range(100))
    assert tree.height > before
    assert tree.root_page_id != old_root, "a root split allocates a new root page"
    assert pager.read_page(tree.root_page_id).page_type is PageType.BTREE_INTERNAL
    assert tree.stats.root_splits >= 1
    tree.verify()


def test_the_old_root_keeps_its_page_id_and_becomes_the_leftmost_child(
    tree: BPlusTree, pager: Pager
):
    # Growth at the root is cheap precisely because nothing has to be rewritten:
    # the old root stays where it is and gains a parent.
    old_root = tree.root_page_id
    fill(tree, range(100))
    root = BTreeNode(pager.read_page(tree.root_page_id))
    assert not root.is_leaf
    descendants = set(tree.page_ids())
    assert old_root in descendants


def test_enough_rows_reach_depth_three(tree: BPlusTree):
    fill(tree, range(1500))
    assert tree.height >= 3, tree.height
    tree.verify()
    assert tree.count() == 1500


def test_all_leaves_stay_at_the_same_depth(pager: Pager):
    # verify() is what checks this; the test exists to name the property, which
    # is the whole reason a B+ tree needs no rebalancing pass.
    for count in (5, 50, 500):
        subject = BPlusTree.create(pager, name=f"d{count}")
        fill(subject, range(count))
        subject.verify()


def test_ascending_inserts_still_balance(tree: BPlusTree):
    # The pathological case for a naive binary tree: sorted input degenerates it
    # into a linked list. A B+ tree splits instead, so height stays logarithmic.
    fill(tree, range(800))
    tree.verify()
    assert tree.height <= 4, tree.height


def test_descending_inserts_still_balance(tree: BPlusTree):
    fill(tree, range(800, 0, -1))
    tree.verify()
    assert tree.height <= 4, tree.height


def test_splits_are_reported_as_events(pager: Pager):
    from engine.diagnostics import RingBufferSink, TraceLevel, Tracer

    sink = RingBufferSink(capacity=4096)
    subject = BPlusTree.create(pager, name="ev", tracer=Tracer(sink, TraceLevel.STORAGE))
    fill(subject, range(200))
    splits = [item.event for item in sink.snapshot() if item.event_type == "NodeSplitEvent"]
    assert splits, "a 256-byte page must have split"
    assert any(event.is_root_split for event in splits)
    assert all(event.new_page_id != event.page_id for event in splits)


# -- duplicates -------------------------------------------------------------


def test_duplicate_keys_are_all_retained(tree: BPlusTree):
    for n in range(50):
        tree.insert(key(7), rid(n))
    tree.verify()
    assert sorted(tree.search(key(7))) == sorted(rid(n) for n in range(50))


def test_duplicates_can_span_several_leaves(tree: BPlusTree):
    # The point of making the record id a tiebreaker: a run of equal keys can be
    # split anywhere, so a page full of one repeated key is still splittable.
    for n in range(400):
        tree.insert(key(7), rid(n))
    tree.verify()
    assert tree.height >= 2
    assert len(tree.search(key(7))) == 400


def test_a_search_steps_right_across_a_leaf_boundary(tree: BPlusTree):
    for n in range(400):
        tree.insert(key(7), rid(n))
    tree.insert(key(1), rid(900))
    tree.insert(key(9), rid(901))
    tree.verify()
    assert len(tree.search(key(7))) == 400
    assert tree.search(key(1)) == [rid(900)]
    assert tree.search(key(9)) == [rid(901)]


def test_duplicates_do_not_break_the_ordered_scan(tree: BPlusTree):
    values = [n % 5 for n in range(200)]
    fill(tree, values)
    tree.verify()
    assert scanned(tree) == sorted(values)


# -- range scans ------------------------------------------------------------


@pytest.mark.parametrize(
    ("low", "high", "include_low", "include_high", "expected"),
    [
        (10, 20, True, True, list(range(10, 21))),
        (10, 20, False, True, list(range(11, 21))),
        (10, 20, True, False, list(range(10, 20))),
        (10, 20, False, False, list(range(11, 20))),
        (None, 5, True, True, list(range(0, 6))),
        (95, None, True, True, list(range(95, 100))),
        (None, None, True, True, list(range(100))),
        (200, 300, True, True, []),
        (50, 50, True, True, [50]),
        (50, 49, True, True, []),
    ],
)
def test_range_bounds(tree: BPlusTree, low, high, include_low, include_high, expected):
    fill(tree, range(100))
    got = [
        decode_key(k, DataType.INTEGER)
        for k, _ in tree.range_scan(
            None if low is None else key(low),
            None if high is None else key(high),
            include_low=include_low,
            include_high=include_high,
        )
    ]
    assert got == expected


def test_a_range_scan_crosses_leaf_links(tree: BPlusTree):
    fill(tree, range(500))
    assert tree.height >= 2
    before = tree.stats.leaves_visited
    got = [decode_key(k, DataType.INTEGER) for k, _ in tree.range_scan(key(100), key(400))]
    assert got == list(range(100, 401))
    assert tree.stats.leaves_visited - before > 1, "the range must span leaves"


def test_the_leaf_chain_reaches_every_leaf(tree: BPlusTree, pager: Pager):
    fill(tree, range(500))
    walked = set()
    node = BTreeNode(pager.read_page(tree.root_page_id))
    while not node.is_leaf:
        node = BTreeNode(pager.read_page(node.entry_at(0).child_page_id))
    while True:
        walked.add(node.page_id)
        if node.next_leaf_id == INVALID_PAGE_ID:
            break
        node = BTreeNode(pager.read_page(node.next_leaf_id))

    every_leaf = {
        page_id
        for page_id in tree.page_ids()
        if BTreeNode(pager.read_page(page_id)).is_leaf
    }
    assert walked == every_leaf


def test_a_range_scan_is_lazy(tree: BPlusTree):
    # `LIMIT 1` over a large matching range must not read the whole range. The
    # generator contract is what the volcano executor relies on end to end.
    fill(tree, range(1000))
    before = tree.stats.leaves_visited
    scan = tree.range_scan(key(0), key(999))
    next(scan)
    assert tree.stats.leaves_visited - before == 1
    scan.close()


def test_abandoning_a_range_scan_leaves_the_tree_usable(tree: BPlusTree):
    # IndexScan.close() closes the generator on every path, including
    # cancellation. Nothing about the tree is held open, so a second scan sees
    # exactly the same thing.
    fill(tree, range(200))
    scan = tree.range_scan()
    next(scan)
    next(scan)
    scan.close()
    assert scanned(tree) == list(range(200))
    tree.verify()


# -- deletion ---------------------------------------------------------------


def test_deleting_removes_only_the_named_entry(tree: BPlusTree):
    fill(tree, range(50))
    assert tree.delete(key(20), rid(20))
    tree.verify()
    assert tree.search(key(20)) == []
    assert tree.count() == 49
    assert scanned(tree) == [v for v in range(50) if v != 20]


def test_deleting_one_duplicate_leaves_the_others(tree: BPlusTree):
    for n in range(20):
        tree.insert(key(7), rid(n))
    assert tree.delete(key(7), rid(5))
    tree.verify()
    assert len(tree.search(key(7))) == 19
    assert rid(5) not in tree.search(key(7))


def test_deleting_a_key_that_is_not_there_returns_false(tree: BPlusTree):
    fill(tree, range(20))
    assert tree.delete(key(999), rid(0)) is False
    assert tree.delete(key(5), RecordId(9999, 0)) is False


def test_delete_key_removes_every_duplicate(tree: BPlusTree):
    for n in range(30):
        tree.insert(key(7), rid(n))
    tree.insert(key(8), rid(100))
    assert tree.delete_key(key(7)) == 30
    tree.verify()
    assert tree.search(key(7)) == []
    assert tree.search(key(8)) == [rid(100)]


def test_emptying_the_tree_leaves_it_usable(tree: BPlusTree):
    # Nothing merges, so the pages stay. The tree must still be correct — and
    # reusable, which is the compensation for never shrinking.
    fill(tree, range(300))
    for position, value in enumerate(range(300)):
        assert tree.delete(key(value), rid(position))
    tree.verify()
    assert tree.count() == 0
    assert list(tree.range_scan()) == []

    fill(tree, range(300))
    tree.verify()
    assert tree.count() == 300


def test_space_from_deletions_is_reused_in_place(tree: BPlusTree):
    fill(tree, range(300))
    pages_before = len(tree.page_ids())
    for position, value in enumerate(range(300)):
        tree.delete(key(value), rid(position))
    fill(tree, range(300))
    assert len(tree.page_ids()) == pages_before, (
        "reinserting the same keys should reuse the emptied nodes"
    )


# -- unique indexes ---------------------------------------------------------


def test_a_unique_index_rejects_a_duplicate(pager: Pager):
    subject = BPlusTree.create(pager, name="u", unique=True)
    subject.insert(key(1), rid(0))
    with pytest.raises(UniqueViolation, match="unique"):
        subject.insert(key(1), rid(1))
    subject.verify()
    assert len(subject.search(key(1))) == 1


def test_a_unique_index_permits_many_nulls(pager: Pager):
    # SQL's rule: two unknowns are not known to be equal, so UNIQUE does not
    # constrain them. PostgreSQL, SQLite and the standard all agree.
    subject = BPlusTree.create(pager, name="u", unique=True)
    for n in range(20):
        subject.insert(key(None), rid(n))
    subject.verify()
    assert len(subject.search(key(None))) == 20


def test_a_non_unique_index_allows_duplicates(tree: BPlusTree):
    tree.insert(key(1), rid(0))
    tree.insert(key(1), rid(1))
    assert len(tree.search(key(1))) == 2


# -- other key types --------------------------------------------------------


def test_text_keys_of_varying_width(pager: Pager):
    subject = BPlusTree.create(pager, name="t", data_type=DataType.TEXT)
    words = [f"{'x' * (n % 30)}{n:04d}" for n in range(250)]
    shuffled = words.copy()
    random.Random(13).shuffle(shuffled)
    fill(subject, shuffled, DataType.TEXT)
    subject.verify()
    assert scanned(subject, DataType.TEXT) == sorted(words)


def test_float_keys(pager: Pager):
    subject = BPlusTree.create(pager, name="f", data_type=DataType.FLOAT)
    values = [n / 7 - 20 for n in range(200)]
    shuffled = values.copy()
    random.Random(17).shuffle(shuffled)
    fill(subject, shuffled, DataType.FLOAT)
    subject.verify()
    assert scanned(subject, DataType.FLOAT) == sorted(values)


def test_nulls_sort_first_and_are_searchable(tree: BPlusTree):
    fill(tree, [None, 5, None, 1, 3])
    tree.verify()
    assert scanned(tree) == [None, None, 1, 3, 5]
    assert len(tree.search(key(None))) == 2


# -- structure --------------------------------------------------------------


def test_every_internal_node_starts_at_minus_infinity(tree: BPlusTree, pager: Pager):
    # The sentinel is what removes the need for a special "leftmost child"
    # header field, so a descent always finds a child to follow.
    fill(tree, range(600))
    for page_id in tree.page_ids():
        node = BTreeNode(pager.read_page(page_id))
        if node.is_leaf:
            continue
        assert node.entry_at(0).is_minus_infinity, page_id


def test_internal_nodes_carry_no_sibling_link(tree: BPlusTree, pager: Pager):
    # Only leaves are chained; nothing walks internal nodes sideways.
    fill(tree, range(600))
    for page_id in tree.page_ids():
        node = BTreeNode(pager.read_page(page_id))
        if not node.is_leaf:
            assert node.next_leaf_id == INVALID_PAGE_ID


def test_a_node_view_refuses_a_heap_page(pager: Pager):
    page = pager.allocate_page(PageType.HEAP)
    with pytest.raises(CorruptPageError, match=r"not a B\+ tree node"):
        BTreeNode(page)


def test_a_cyclic_page_chain_is_caught_not_followed(tree: BPlusTree, pager: Pager):
    fill(tree, range(600))
    root = BTreeNode(pager.read_page(tree.root_page_id))
    # Point the root's second child back at the root.
    entry = root.entry_at(1)
    root.delete_entry_at(1)
    root.insert_entry_at(1, Entry(entry.key, entry.record_id, tree.root_page_id))
    pager.write_page(root.page)
    with pytest.raises(CorruptPageError, match=r"twice|cyclic"):
        tree.page_ids()


def test_verify_catches_an_unsorted_node(tree: BPlusTree, pager: Pager):
    fill(tree, range(20))
    leaf = BTreeNode(pager.read_page(tree.root_page_id))
    first = leaf.entry_at(0)
    leaf.delete_entry_at(0)
    # Put the smallest key at the end, breaking slot order.
    leaf.insert_entry_at(leaf.count, first)
    pager.write_page(leaf.page)
    with pytest.raises(IndexingError, match="not sorted"):
        tree.verify()


# -- the split-point calculation -------------------------------------------


def test_plan_split_balances_by_bytes_not_by_count():
    # One huge entry then many small ones: cutting by count would leave the left
    # page nearly full and the right nearly empty.
    widths = [100, *([4] * 20)]
    cut = plan_split(widths, capacity=200)
    assert sum(widths[:cut]) <= 200
    assert sum(widths[cut:]) <= 200
    assert cut < len(widths) // 2, "the byte-heavy left side should be cut early"


def test_plan_split_always_leaves_both_sides_non_empty():
    for count in range(2, 40):
        widths = [10] * count
        cut = plan_split(widths, capacity=200)
        assert 1 <= cut <= count - 1


def test_plan_split_respects_capacity_at_the_extremes():
    # Two entries that each nearly fill a page: the only valid cut is down the
    # middle, and the byte-balanced point happens to agree.
    widths = [120, 120]
    assert plan_split(widths, capacity=128) == 1


def test_plan_split_refuses_a_node_it_cannot_split():
    with pytest.raises(IndexingError, match="cannot split"):
        plan_split([10], capacity=100)


# -- statistics -------------------------------------------------------------


def test_stats_count_what_happened(tree: BPlusTree):
    fill(tree, range(200))
    tree.search(key(50))
    list(tree.range_scan(key(10), key(20)))
    tree.delete(key(10), rid(10))
    stats = tree.stats
    assert stats.inserts == 200
    assert stats.searches == 1
    assert stats.range_scans >= 1
    assert stats.deletes == 1
    assert stats.splits > 0
    assert stats.pages_allocated == len(tree.page_ids())
    assert set(stats.as_dict()) >= {"inserts", "splits", "root_splits"}


def test_a_point_lookup_reads_far_fewer_pages_than_the_tree_holds(tree: BPlusTree):
    fill(tree, range(1500))
    pages = len(tree.page_ids())
    before = tree.stats.nodes_visited
    tree.search(key(750))
    visited = tree.stats.nodes_visited - before
    assert visited <= tree.height + 1
    assert visited * 10 < pages, (
        f"a lookup touched {visited} of {pages} pages — that ratio is the "
        f"entire argument for building an index"
    )
