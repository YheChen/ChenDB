"""Storage introspection: the data behind the visualizer's page inspector."""

from __future__ import annotations

from pathlib import Path

from engine.database import Database
from engine.serialization.schema import Schema
from engine.storage.constants import FORMAT_VERSION, META_PAGE_ID, PageType
from engine.storage.inspect import hexdump, render_page_map
from engine.storage.page import PAGE_HEADER_SIZE, SLOT_SIZE

PAGE_SIZE = 256


def test_every_page_is_summarised_and_attributed(db: Database, sample_rows):
    db.insert_many("users", sample_rows)
    summaries = db.page_summaries()

    assert [s.page_id for s in summaries] == list(range(db.page_count))
    by_owner = {s.page_id: s.owner for s in summaries}
    assert by_owner[META_PAGE_ID] == "meta"
    # Every page now belongs to a named table — including the catalog's own.
    assert "chendb_tables" in by_owner.values()
    assert "chendb_columns" in by_owner.values()
    assert "users" in by_owner.values()
    assert all(s.checksum_valid for s in summaries)


def test_page_summary_reports_real_free_space(db: Database):
    db.insert("users", (1, "row", None, True, 0.0))
    heap_page_id = min(db.heap_page_ids("users"))
    summary = next(s for s in db.page_summaries() if s.page_id == heap_page_id)

    page = db.read_page(heap_page_id)
    assert summary.free_space == page.free_space
    assert summary.slot_count == page.slot_count == 1
    assert summary.live_record_count == 1
    assert summary.page_type == PageType.HEAP.name


def test_file_offsets_are_page_id_times_page_size(db: Database):
    for summary in db.page_summaries():
        assert summary.file_offset == summary.page_id * PAGE_SIZE


def test_meta_page_detail_decodes_its_own_header(db: Database):
    detail = db.page_detail(META_PAGE_ID)
    fields = {field.name: field for field in detail.header_fields}

    assert fields["magic"].value.startswith("ChenDB")
    assert fields["page_size"].value == PAGE_SIZE
    assert fields["format_version"].value == FORMAT_VERSION
    # The catalog's bootstrap pointers are the reason the meta page exists.
    assert fields["catalog_tables_first"].value > 0
    assert fields["catalog_columns_first"].value > 0
    assert detail.slots == ()
    assert detail.summary.checksum_valid


def test_heap_page_detail_decodes_records_and_their_field_offsets(db: Database):
    db.insert("users", (7, "Ada", 36, True, 1.5))
    heap_page_id = min(db.heap_page_ids("users"))
    detail = db.page_detail(heap_page_id)

    assert len(detail.slots) == 1
    slot = detail.slots[0]
    assert slot.is_live
    assert slot.decode_error is None
    assert slot.record is not None
    assert slot.record.values == (7, "Ada", 36, True, 1.5)
    assert slot.raw_hex  # raw bytes are available next to the decoded view

    # Field offsets must point inside the record and be strictly increasing.
    offsets = [f.offset for f in slot.record.fields if not f.is_null]
    assert offsets == sorted(offsets)
    assert min(offsets) >= slot.record.null_bitmap_size


def test_deleted_slots_are_reported_as_tombstones(db: Database):
    record_id = db.insert("users", (1, "gone", None, True, 0.0))
    db.insert("users", (2, "stays", None, True, 0.0))
    db.delete("users", record_id)

    detail = db.page_detail(record_id.page_id)
    tombstone = detail.slots[record_id.slot_id]
    assert tombstone.is_live is False
    assert tombstone.record is None
    assert tombstone.raw_hex == ""
    # The slot entry survives, so later record ids do not shift.
    assert len(detail.slots) == 2


def test_page_regions_tile_the_whole_page(db: Database):
    db.insert("users", (1, "x", None, True, 0.0))
    detail = db.page_detail(min(db.heap_page_ids("users")))

    assert detail.header_size == PAGE_HEADER_SIZE
    assert detail.slot_directory_end == PAGE_HEADER_SIZE + len(detail.slots) * SLOT_SIZE
    assert detail.free_start == detail.slot_directory_end
    assert detail.free_start <= detail.free_end <= detail.page_size
    assert len(detail.raw) == detail.page_size


def test_a_corrupt_page_still_renders_instead_of_raising(
    db_path: Path, users_schema: Schema
):
    with Database.open(db_path, page_size=PAGE_SIZE) as db:
        db.create_table("users", users_schema)
        record_id = db.insert("users", (1, "victim", None, True, 0.0))
        heap_page_id = record_id.page_id

    raw = bytearray(db_path.read_bytes())
    raw[heap_page_id * PAGE_SIZE + 30] ^= 0xFF
    db_path.write_bytes(bytes(raw))

    with Database.open(db_path, verify_checksums=False) as db:
        summary = next(
            s for s in db.page_summaries() if s.page_id == heap_page_id
        )
        # The inspector's job is to *show* corruption, not to fall over on it.
        assert summary.checksum_valid is False


def test_hexdump_formats_offset_hex_and_ascii():
    dump = hexdump(b"ChenDB\x00\xff" + bytes(8), start_offset=4096)
    assert dump.startswith("00001000  43 68 65 6e 44 42 00 ff")
    assert dump.endswith("|ChenDB..........|")


def test_hexdump_truncates_and_says_so():
    dump = hexdump(bytes(4096), limit=32)
    assert "4064 more bytes" in dump


def test_page_map_renders_all_four_regions(db: Database):
    db.insert("users", (1, "x", None, True, 0.0))
    rendered = render_page_map(db.page_detail(min(db.heap_page_ids("users"))))
    for region in ("header", "slot directory", "free space", "record data"):
        assert region in rendered
