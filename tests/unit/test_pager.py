"""Pager, meta page, and the page allocator."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.errors import (
    ChecksumMismatchError,
    CorruptDatabaseError,
    PageNotFoundError,
)
from engine.storage.constants import INVALID_PAGE_ID, MAGIC, META_PAGE_ID, PageType
from engine.storage.meta import META_HEADER_SIZE, MetaPage
from engine.storage.pager import Pager

PAGE_SIZE = 256


# -- creation and reopening ------------------------------------------------


def test_new_file_contains_one_page_and_starts_with_the_magic(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        assert pager.page_count == 1
        assert pager.page_size == PAGE_SIZE

    assert db_path.stat().st_size == PAGE_SIZE
    assert db_path.read_bytes()[:16] == MAGIC


def test_reopening_recovers_page_size_and_count(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        pager.allocate_page(PageType.HEAP)
        pager.allocate_page(PageType.HEAP)

    with Pager(db_path) as pager:
        assert pager.page_size == PAGE_SIZE
        assert pager.page_count == 3


def test_reopening_with_a_conflicting_page_size_is_an_error(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE):
        pass
    with pytest.raises(CorruptDatabaseError, match="cannot reopen"):
        Pager(db_path, page_size=1024)


def test_opening_a_missing_file_without_create_fails(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        Pager(tmp_path / "absent.chendb", create=False)


def test_a_non_chendb_file_is_rejected(tmp_path: Path):
    path = tmp_path / "not-a-db.chendb"
    path.write_bytes(b"this is a text file, definitely not a database" * 10)
    with pytest.raises(CorruptDatabaseError, match="bad magic"):
        Pager(path)


def test_truncated_file_is_detected_on_open(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        pager.allocate_page(PageType.HEAP)
        pager.allocate_page(PageType.HEAP)

    # Simulate a filesystem losing the tail of the file.
    raw = db_path.read_bytes()
    db_path.write_bytes(raw[: len(raw) - PAGE_SIZE])

    with pytest.raises(CorruptDatabaseError, match="truncated or partially written"):
        Pager(db_path)


def test_a_corrupt_meta_page_is_detected(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE):
        pass
    raw = bytearray(db_path.read_bytes())
    raw[24] ^= 0xFF  # page_count field, inside the checksum's coverage
    db_path.write_bytes(bytes(raw))

    with pytest.raises(ChecksumMismatchError, match="meta page"):
        Pager(db_path)


# -- allocation ------------------------------------------------------------


def test_allocation_extends_the_file_by_exactly_one_page(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        page = pager.allocate_page(PageType.HEAP)
        assert page.page_id == 1
        assert pager.page_count == 2
        assert pager.file_offset(1) == PAGE_SIZE
    assert db_path.stat().st_size == 2 * PAGE_SIZE


def test_allocated_pages_are_zeroed_and_typed(pager: Pager):
    page = pager.allocate_page(PageType.HEAP)
    assert page.page_type is PageType.HEAP
    assert page.live_record_count == 0
    assert page.next_page_id == INVALID_PAGE_ID


def test_write_then_read_a_page_roundtrips(pager: Pager):
    page = pager.allocate_page(PageType.HEAP)
    page.insert(b"durable")
    pager.write_page(page)

    reloaded = pager.read_page(page.page_id)
    assert reloaded.read(0) == b"durable"


def test_reading_beyond_the_end_of_the_file_is_an_error(pager: Pager):
    with pytest.raises(PageNotFoundError, match="outside the file"):
        pager.read_page(99)


def test_the_meta_page_is_not_reachable_as_a_slotted_page(pager: Pager):
    with pytest.raises(ValueError, match="meta page"):
        pager.read_page(META_PAGE_ID)
    # ...but its raw bytes are, for the inspector.
    assert pager.read_raw(META_PAGE_ID)[:16] == MAGIC


def test_a_torn_data_page_is_detected_on_read(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        page = pager.allocate_page(PageType.HEAP)
        page.insert(b"important")
        pager.write_page(page)

    raw = bytearray(db_path.read_bytes())
    raw[PAGE_SIZE + 100] ^= 0xFF  # flip a bit inside page 1
    db_path.write_bytes(bytes(raw))

    with Pager(db_path) as pager, pytest.raises(ChecksumMismatchError, match="page 1"):
        pager.read_page(1)


def test_checksum_verification_can_be_disabled_for_forensics(db_path: Path):
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        page = pager.allocate_page(PageType.HEAP)
        page.insert(b"important")
        pager.write_page(page)

    raw = bytearray(db_path.read_bytes())
    raw[PAGE_SIZE + 100] ^= 0xFF
    db_path.write_bytes(bytes(raw))

    with Pager(db_path, verify_checksums=False) as pager:
        assert pager.read_page(1) is not None


# -- free list -------------------------------------------------------------


def test_freed_pages_are_recycled_instead_of_growing_the_file(pager: Pager):
    first = pager.allocate_page(PageType.HEAP)
    second = pager.allocate_page(PageType.HEAP)
    page_count_before = pager.page_count

    pager.free_page(first.page_id)
    recycled = pager.allocate_page(PageType.HEAP)

    assert recycled.page_id == first.page_id
    assert pager.page_count == page_count_before
    assert pager.stats.recycled_allocations == 1
    assert second.page_id != recycled.page_id


def test_the_free_list_is_last_in_first_out(pager: Pager):
    pages = [pager.allocate_page(PageType.HEAP).page_id for _ in range(3)]
    for page_id in pages:
        pager.free_page(page_id)

    assert list(pager.free_list()) == list(reversed(pages))
    assert [pager.allocate_page(PageType.HEAP).page_id for _ in range(3)] == list(
        reversed(pages)
    )
    assert list(pager.free_list()) == []


def test_a_freed_page_is_wiped(pager: Pager):
    page = pager.allocate_page(PageType.HEAP)
    page.insert(b"CONFIDENTIAL")
    pager.write_page(page)

    pager.free_page(page.page_id)

    assert b"CONFIDENTIAL" not in pager.read_raw(page.page_id)


def test_the_meta_page_cannot_be_freed(pager: Pager):
    with pytest.raises(ValueError, match="meta page"):
        pager.free_page(META_PAGE_ID)


# -- statistics and durability ---------------------------------------------


def test_stats_count_logical_reads_separately_from_syscalls(db_path: Path):
    # Before Milestone 7 these were one number. The gap between them is the
    # buffer pool doing its job.
    with Pager(db_path, page_size=PAGE_SIZE) as pager:
        page = pager.allocate_page(PageType.HEAP)
        reads_before = pager.stats.page_reads
        physical_before = pager.stats.physical_reads
        pager.read_page(page.page_id)
        pager.read_page(page.page_id)

        assert pager.stats.page_reads == reads_before + 2, "both were asked for"
        assert pager.stats.physical_reads == physical_before, (
            "the page was already resident — neither read touched the file"
        )
        assert pager.stats.bytes_read == pager.stats.physical_reads * PAGE_SIZE
        assert pager.stats.allocations == 1


def test_close_is_idempotent(db_path: Path):
    pager = Pager(db_path, page_size=PAGE_SIZE)
    pager.close()
    pager.close()
    assert pager.closed
    with pytest.raises(ValueError, match="closed"):
        pager.read_raw(0)


def test_sync_makes_writes_durable_across_a_reopen(db_path: Path):
    pager = Pager(db_path, page_size=PAGE_SIZE)
    page = pager.allocate_page(PageType.HEAP)
    page.insert(b"survives")
    pager.write_page(page)
    pager.sync()
    # Deliberately do not close: prove sync alone is sufficient.
    with Pager(db_path) as reopened:
        assert reopened.read_page(1).read(0) == b"survives"
    pager.close()


# -- meta page unit tests --------------------------------------------------


def test_meta_page_roundtrip():
    meta = MetaPage(
        page_size=PAGE_SIZE,
        page_count=7,
        free_list_head=3,
        catalog_tables_first=1,
        catalog_tables_last=2,
        catalog_columns_first=3,
        catalog_columns_last=4,
        next_object_id=100,
        lsn=12345,
        flags=1,
    )
    assert MetaPage.from_bytes(meta.to_bytes()) == meta


def test_meta_header_is_88_bytes_and_the_rest_is_reserved():
    assert META_HEADER_SIZE == 88
    raw = MetaPage(page_size=PAGE_SIZE).to_bytes()
    assert len(raw) == PAGE_SIZE
    assert raw[META_HEADER_SIZE:] == bytes(PAGE_SIZE - META_HEADER_SIZE)


def test_meta_page_rejects_a_future_format_version():
    raw = bytearray(MetaPage(page_size=PAGE_SIZE).to_bytes())
    raw[16:20] = (999).to_bytes(4, "little")
    with pytest.raises(CorruptDatabaseError, match="format version 999"):
        MetaPage.from_bytes(bytes(raw), verify_checksum=False)


def test_a_version_1_file_is_rejected_with_an_explanation():
    # Version 1 predates the catalog and cannot be upgraded in place. Saying so
    # beats "unsupported version" or, worse, reinterpreting the bytes.
    raw = bytearray(MetaPage(page_size=PAGE_SIZE).to_bytes())
    raw[16:20] = (1).to_bytes(4, "little")
    with pytest.raises(CorruptDatabaseError, match="Milestone 4"):
        MetaPage.from_bytes(bytes(raw), verify_checksum=False)


def test_a_version_2_file_is_rejected_with_an_explanation():
    # Version 2 predates chendb_indexes, so its meta page is 8 bytes shorter and
    # has no index pointers to read. Same reasoning as version 1: refuse, and
    # say which milestone moved the goalposts.
    raw = bytearray(MetaPage(page_size=PAGE_SIZE).to_bytes())
    raw[16:20] = (2).to_bytes(4, "little")
    with pytest.raises(CorruptDatabaseError, match="Milestone 5"):
        MetaPage.from_bytes(bytes(raw), verify_checksum=False)
