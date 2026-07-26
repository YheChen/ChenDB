"""Slotted page unit tests."""

from __future__ import annotations

import pytest

from engine.errors import ChecksumMismatchError, CorruptPageError, RecordTooLargeError
from engine.storage.constants import INVALID_PAGE_ID, PageType
from engine.storage.page import PAGE_HEADER_SIZE, SLOT_SIZE, Page, validate_page_size

PAGE_SIZE = 256


def make_page(page_size: int = PAGE_SIZE) -> Page:
    return Page.create(page_id=1, page_type=PageType.HEAP, page_size=page_size)


# -- layout constants ------------------------------------------------------


def test_header_and_slot_sizes_are_frozen_by_the_file_format():
    # These are on-disk constants. Changing them requires a format bump, so a
    # test pins them against accidental edits.
    assert PAGE_HEADER_SIZE == 24
    assert SLOT_SIZE == 4


def test_new_page_starts_empty_with_free_space_between_the_two_regions():
    page = make_page()
    assert page.page_type is PageType.HEAP
    assert page.slot_count == 0
    assert page.free_start == PAGE_HEADER_SIZE
    assert page.free_end == PAGE_SIZE
    assert page.free_space == PAGE_SIZE - PAGE_HEADER_SIZE
    assert page.next_page_id == INVALID_PAGE_ID
    assert page.live_record_count == 0


@pytest.mark.parametrize("bad_size", [0, 100, 128 + 1, 3000, 1 << 20])
def test_page_size_must_be_a_power_of_two_in_range(bad_size: int):
    with pytest.raises(ValueError):
        validate_page_size(bad_size)


# -- insert / read ---------------------------------------------------------


def test_insert_then_read_roundtrips():
    page = make_page()
    slot_id = page.insert(b"hello")
    assert slot_id == 0
    assert page.read(0) == b"hello"


def test_records_are_laid_out_backwards_from_the_end_of_the_page():
    page = make_page()
    page.insert(b"aaa")
    page.insert(b"bb")
    first, second = page.slot_directory()
    # Slot 0 sits highest in the page; each later record is placed below it.
    assert first.offset == PAGE_SIZE - 3
    assert second.offset == PAGE_SIZE - 3 - 2
    assert page.free_end == second.offset


def test_insert_consumes_payload_plus_one_slot_entry():
    page = make_page()
    before = page.free_space
    page.insert(b"1234")
    assert page.free_space == before - 4 - SLOT_SIZE


def test_insert_returns_none_when_the_page_is_full():
    page = make_page()
    payload = b"x" * 32
    inserted = 0
    while page.insert(payload) is not None:
        inserted += 1
    assert inserted > 0
    assert page.insert(payload) is None
    # The page is full but still structurally valid.
    page.validate()


def test_record_larger_than_a_page_raises_rather_than_returning_none():
    page = make_page()
    with pytest.raises(RecordTooLargeError, match="exceeds"):
        page.insert(b"x" * (page.max_payload_size + 1))


def test_max_payload_exactly_fits_an_empty_page():
    page = make_page()
    assert page.insert(b"x" * page.max_payload_size) == 0
    assert page.free_space == 0


def test_empty_payload_is_a_valid_record():
    page = make_page()
    slot_id = page.insert(b"")
    assert page.read(slot_id) == b""
    assert page.live_record_count == 1


def test_read_out_of_range_slot_returns_none():
    page = make_page()
    assert page.read(0) is None
    assert page.read(-1) is None
    assert page.read(999) is None


# -- delete ----------------------------------------------------------------


def test_delete_tombstones_the_slot_and_is_not_idempotent_in_its_return():
    page = make_page()
    page.insert(b"gone")
    assert page.delete(0) is True
    assert page.delete(0) is False
    assert page.read(0) is None
    assert page.live_record_count == 0
    # The slot entry survives so later slot ids do not shift.
    assert page.slot_count == 1


def test_delete_does_not_immediately_reclaim_space():
    page = make_page()
    page.insert(b"x" * 40)
    free_after_insert = page.free_space
    page.delete(0)
    assert page.free_space == free_after_insert
    assert page.reclaimable_space == 40 + SLOT_SIZE


def test_deleted_slots_are_reused_before_the_directory_grows():
    page = make_page()
    page.insert(b"aaaa")
    page.insert(b"bbbb")
    page.delete(0)
    assert page.insert(b"cccc") == 0
    assert page.slot_count == 2
    assert page.read(0) == b"cccc"


def test_iter_records_skips_tombstones():
    page = make_page()
    for payload in (b"a", b"b", b"c"):
        page.insert(payload)
    page.delete(1)
    assert list(page.iter_records()) == [(0, b"a"), (2, b"c")]


# -- compaction ------------------------------------------------------------


def test_compaction_reclaims_dead_space_and_preserves_slot_ids():
    page = make_page()
    page.insert(b"x" * 30)  # slot 0
    page.insert(b"y" * 30)  # slot 1
    page.insert(b"z" * 30)  # slot 2
    page.delete(1)

    reclaimed = page.compact()

    assert reclaimed == 30
    # The surviving records keep their original slot numbers — the whole
    # reason external RecordIds stay valid across compaction.
    assert page.read(0) == b"x" * 30
    assert page.read(1) is None
    assert page.read(2) == b"z" * 30
    page.validate()


def test_compaction_trims_trailing_dead_slots_only():
    page = make_page()
    for _ in range(4):
        page.insert(b"abcd")
    page.delete(1)  # interior — must be kept so slot 2 and 3 do not renumber
    page.delete(3)  # trailing — can be dropped
    page.compact()
    assert page.slot_count == 3
    assert page.read(0) == b"abcd"
    assert page.read(1) is None
    assert page.read(2) == b"abcd"


def test_compaction_of_a_clean_page_reclaims_nothing():
    page = make_page()
    page.insert(b"abc")
    assert page.compact() == 0
    assert page.read(0) == b"abc"


def test_compaction_zeroes_the_bytes_it_abandons():
    page = make_page()
    page.insert(b"SECRET-VALUE")
    page.delete(0)
    page.compact()
    assert b"SECRET" not in bytes(page.raw)


def test_would_fit_predicates_agree_with_insert():
    page = make_page()
    filler = b"z" * 50
    while page.would_fit(len(filler)):
        assert page.insert(filler) is not None
    assert page.insert(filler) is None

    page.delete(0)
    assert page.would_fit(len(filler)) is False
    assert page.would_fit_after_compaction(len(filler)) is True


# -- checksums -------------------------------------------------------------


def test_checksum_roundtrips_through_bytes():
    page = make_page()
    page.insert(b"payload")
    raw = page.to_bytes()
    restored = Page.from_bytes(1, raw, PAGE_SIZE)
    assert restored.read(0) == b"payload"
    restored.verify_checksum()


def test_a_single_flipped_bit_is_detected():
    page = make_page()
    page.insert(b"payload")
    raw = bytearray(page.to_bytes())
    raw[PAGE_SIZE - 1] ^= 0b0000_0001

    with pytest.raises(ChecksumMismatchError, match="checksum mismatch"):
        Page.from_bytes(1, bytes(raw), PAGE_SIZE)


def test_corruption_can_be_inspected_when_verification_is_skipped():
    page = make_page()
    page.insert(b"payload")
    raw = bytearray(page.to_bytes())
    raw[PAGE_SIZE - 1] ^= 0xFF
    # The inspector must still be able to show a damaged page.
    damaged = Page.from_bytes(1, bytes(raw), PAGE_SIZE, verify_checksum=False)
    assert damaged.checksum != damaged.compute_checksum()


# -- validation ------------------------------------------------------------


def test_validate_rejects_free_start_that_disagrees_with_slot_count():
    page = make_page()
    page.insert(b"abc")
    page.free_start = PAGE_HEADER_SIZE  # lie: says zero slots
    with pytest.raises(CorruptPageError, match="free_start"):
        page.validate()


def test_validate_rejects_a_slot_pointing_outside_the_record_region():
    page = make_page()
    page.insert(b"abc")
    page._write_slot(0, PAGE_SIZE - 1, 99)  # runs off the end of the page
    with pytest.raises(CorruptPageError, match="outside the record region"):
        page.validate()


def test_unknown_page_type_is_reported_as_corruption():
    page = make_page()
    page._set_u8(12, 200)
    with pytest.raises(CorruptPageError, match="unknown page type"):
        _ = page.page_type


def test_buffer_of_the_wrong_length_is_rejected():
    with pytest.raises(CorruptPageError, match="expected"):
        Page(1, bytearray(10), PAGE_SIZE)


# -- stress ----------------------------------------------------------------


def test_fill_delete_refill_cycles_keep_the_page_consistent():
    # Churn the page repeatedly; the invariants must hold at every step and no
    # space may leak.
    page = Page.create(1, PageType.HEAP, 4096)
    for cycle in range(20):
        payload = bytes([cycle % 256]) * (17 + cycle % 40)
        slots = []
        while (slot_id := page.insert(payload)) is not None:
            slots.append(slot_id)
        assert slots, f"cycle {cycle} inserted nothing"
        page.validate()
        for slot_id in slots:
            assert page.read(slot_id) == payload
        for slot_id in slots:
            page.delete(slot_id)
        page.compact()
        page.validate()
        assert page.live_record_count == 0
        assert page.slot_count == 0
        assert page.free_space == 4096 - PAGE_HEADER_SIZE
