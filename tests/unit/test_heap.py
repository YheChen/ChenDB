"""Heap file: page chaining, insert policy, scan and delete."""

from __future__ import annotations

import pytest

from engine.errors import RecordNotFoundError, RecordTooLargeError
from engine.storage.constants import INVALID_PAGE_ID, PageType
from engine.storage.heap import HeapFile, RecordId
from engine.storage.pager import Pager

PAGE_SIZE = 256


@pytest.fixture
def heap(pager: Pager) -> HeapFile:
    return HeapFile.create(pager)


def test_a_new_heap_owns_exactly_one_page(heap: HeapFile, pager: Pager):
    assert heap.first_page_id == heap.last_page_id
    assert list(heap.page_ids()) == [heap.first_page_id]
    assert pager.read_page(heap.first_page_id).page_type is PageType.HEAP


def test_insert_returns_a_usable_address(heap: HeapFile):
    record_id = heap.insert(b"first")
    assert record_id == RecordId(heap.first_page_id, 0)
    assert heap.get(record_id) == b"first"


def test_record_ids_render_like_a_ctid(heap: HeapFile):
    assert str(heap.insert(b"x")) == f"({heap.first_page_id},0)"


def test_inserts_spill_onto_new_linked_pages(heap: HeapFile):
    payload = b"y" * 60
    record_ids = [heap.insert(payload) for _ in range(20)]

    pages = list(heap.page_ids())
    assert len(pages) > 1, "20 x 60B should not fit on one 256B page"
    assert heap.last_page_id == pages[-1]
    # Every record must still be readable at its recorded address.
    for record_id in record_ids:
        assert heap.get(record_id) == payload


def test_the_page_chain_terminates(heap: HeapFile, pager: Pager):
    for _ in range(20):
        heap.insert(b"z" * 60)
    last = pager.read_page(heap.last_page_id)
    assert last.next_page_id == INVALID_PAGE_ID


def test_appending_is_constant_cost_in_pages(heap: HeapFile, pager: Pager):
    # An append must touch the tail page only, never walk the chain. If it
    # walked, reads would grow with the number of pages.
    for _ in range(30):
        heap.insert(b"w" * 60)
    reads_before = pager.stats.page_reads
    heap.insert(b"w" * 60)
    assert pager.stats.page_reads - reads_before <= 1


def test_scan_returns_every_live_record(heap: HeapFile):
    payloads = [f"row-{i:03d}".encode() * 4 for i in range(40)]
    for payload in payloads:
        heap.insert(payload)

    scanned = [payload for _, payload in heap.scan()]
    assert scanned == payloads
    assert heap.count() == len(payloads)


def test_scan_is_lazy(heap: HeapFile, pager: Pager):
    for _ in range(30):
        heap.insert(b"q" * 60)
    reads_before = pager.stats.page_reads

    iterator = heap.scan()
    next(iterator)

    # Pulling one row must not read the whole chain.
    assert pager.stats.page_reads - reads_before == 1


def test_delete_removes_a_row_from_scans(heap: HeapFile):
    ids = [heap.insert(f"row{i}".encode()) for i in range(5)]
    assert heap.delete(ids[2]) is True

    remaining = [record_id for record_id, _ in heap.scan()]
    assert ids[2] not in remaining
    assert len(remaining) == 4
    assert heap.count() == 4


def test_deleting_twice_reports_the_second_as_a_no_op(heap: HeapFile):
    record_id = heap.insert(b"once")
    assert heap.delete(record_id) is True
    assert heap.delete(record_id) is False


def test_reading_a_deleted_record_raises(heap: HeapFile):
    record_id = heap.insert(b"gone")
    heap.delete(record_id)
    with pytest.raises(RecordNotFoundError, match="no live record"):
        heap.get(record_id)


def test_a_record_too_large_for_any_page_is_rejected_with_a_useful_message(
    heap: HeapFile,
):
    with pytest.raises(RecordTooLargeError, match=r"TOAST|overflow"):
        heap.insert(b"x" * (PAGE_SIZE * 2))


def test_deleted_space_is_reused_within_a_page(heap: HeapFile, pager: Pager):
    # Fill page 1, delete everything on it, then insert again. The heap should
    # compact and reuse the page rather than extend the chain.
    payload = b"m" * 50
    ids = []
    while heap.last_page_id == heap.first_page_id:
        ids.append(heap.insert(payload))
    filled_page_ids = [rid for rid in ids if rid.page_id == heap.first_page_id]
    for record_id in filled_page_ids:
        heap.delete(record_id)

    pages_before = heap.page_count()
    compactions_before = heap.stats.compactions
    heap.insert(payload)

    assert heap.page_count() == pages_before
    assert heap.stats.compactions >= compactions_before


def test_on_pages_changed_fires_only_when_the_chain_grows(pager: Pager):
    calls: list[tuple[int, int]] = []
    heap = HeapFile.create(pager, on_pages_changed=lambda first, last: calls.append((first, last)))
    assert calls == [(heap.first_page_id, heap.first_page_id)]

    heap.insert(b"small")
    assert len(calls) == 1, "an insert that fits must not touch the meta page"

    while len(calls) == 1:
        heap.insert(b"n" * 60)
    assert calls[-1] == (heap.first_page_id, heap.last_page_id)


def test_stats_track_work_done(heap: HeapFile):
    heap.insert(b"a")
    heap.insert(b"b")
    record_id = heap.insert(b"c")
    heap.get(record_id)
    heap.delete(record_id)
    list(heap.scan())

    assert heap.stats.inserts == 3
    assert heap.stats.reads == 1
    assert heap.stats.deletes == 1
    assert heap.stats.scans == 1


def test_heap_survives_reopening_the_pager(db_path):
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        heap = HeapFile.create(pager)
        first, last = heap.first_page_id, heap.last_page_id
        for i in range(25):
            heap.insert(f"persisted-{i}".encode())
        first, last = heap.first_page_id, heap.last_page_id

    with Pager(db_path) as pager:
        reopened = HeapFile(pager, first, last)
        assert [payload for _, payload in reopened.scan()] == [
            f"persisted-{i}".encode() for i in range(25)
        ]


def test_many_pages_stress(db_path):
    # 500 rows at 60 bytes each across 256-byte pages is roughly 170 pages.
    row_count = 500
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        heap = HeapFile.create(pager)
        ids = [heap.insert(f"{i:056d}".encode()) for i in range(row_count)]

        assert heap.count() == row_count
        assert heap.page_count() > 100
        assert len(set(ids)) == row_count

        # Delete every third row, then verify the survivors.
        for record_id in ids[::3]:
            heap.delete(record_id)
        survivors = {payload for _, payload in heap.scan()}
        assert len(survivors) == row_count - len(ids[::3])
        assert heap.count() == len(survivors)
