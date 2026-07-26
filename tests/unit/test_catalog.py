"""The system catalog: bootstrap, lookup, caching, and what it refuses."""

from __future__ import annotations

import pytest

from engine.catalog.catalog import Catalog
from engine.catalog.system import (
    COLUMNS_TABLE_ID,
    COLUMNS_TABLE_NAME,
    COLUMNS_TABLE_SCHEMA,
    FIRST_USER_OBJECT_ID,
    INDEXES_TABLE_NAME,
    TABLES_TABLE_ID,
    TABLES_TABLE_NAME,
    TABLES_TABLE_SCHEMA,
    is_system_table,
)
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.errors import CatalogError, CorruptDatabaseError
from engine.serialization.record import decode_record
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.storage.constants import INVALID_PAGE_ID
from engine.storage.pager import Pager

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("age", DataType.INTEGER),
)


@pytest.fixture
def catalog(pager: Pager) -> Catalog:
    instance = Catalog(pager)
    instance.bootstrap()
    return instance


# -- the bootstrap ---------------------------------------------------------


def test_a_fresh_pager_has_no_catalog(pager: Pager):
    assert Catalog(pager).initialised is False
    assert pager.meta.catalog_tables_first == INVALID_PAGE_ID


def test_bootstrap_creates_two_heaps_and_records_them(pager: Pager):
    instance = Catalog(pager)
    instance.bootstrap()

    meta = pager.meta
    assert instance.initialised
    assert meta.catalog_tables_first != INVALID_PAGE_ID
    assert meta.catalog_columns_first != INVALID_PAGE_ID
    assert meta.catalog_tables_first != meta.catalog_columns_first
    assert meta.next_object_id == FIRST_USER_OBJECT_ID


def test_bootstrapping_twice_is_refused(catalog: Catalog):
    with pytest.raises(CatalogError, match="already initialised"):
        catalog.bootstrap()


def test_using_an_uninitialised_catalog_is_a_clear_error(pager: Pager):
    instance = Catalog(pager)
    with pytest.raises(CorruptDatabaseError, match="not initialised"):
        instance.list_tables()


def test_the_system_tables_schemas_are_compiled_in_not_stored(catalog: Catalog):
    """The bootstrap problem: chendb_tables cannot describe itself.

    Its schema comes from code, and its page pointers from the meta page, so
    there is no row to read and no way for two sources to disagree.
    """
    info = catalog.get_table(TABLES_TABLE_NAME)
    assert info is not None
    assert info.schema is TABLES_TABLE_SCHEMA
    assert info.table_id == TABLES_TABLE_ID
    assert info.is_system is True

    # And chendb_tables holds no row about itself.
    for _, payload in catalog.tables_heap.scan():
        row = decode_record(TABLES_TABLE_SCHEMA, payload)
        assert row[1] != TABLES_TABLE_NAME


def test_both_system_tables_are_reachable(catalog: Catalog):
    columns = catalog.get_table(COLUMNS_TABLE_NAME)
    assert columns is not None
    assert columns.table_id == COLUMNS_TABLE_ID
    assert columns.schema is COLUMNS_TABLE_SCHEMA


# -- creating --------------------------------------------------------------


def test_creating_a_table_writes_rows_to_both_system_tables(catalog: Catalog):
    info = catalog.create_table("users", SCHEMA)

    table_rows = [
        decode_record(TABLES_TABLE_SCHEMA, payload)
        for _, payload in catalog.tables_heap.scan()
    ]
    assert len(table_rows) == 1
    table_id, name, first_page, last_page = table_rows[0]
    assert (table_id, name) == (info.table_id, "users")
    assert first_page == info.first_page
    assert last_page == info.last_page

    column_rows = [
        decode_record(COLUMNS_TABLE_SCHEMA, payload)
        for _, payload in catalog.columns_heap.scan()
    ]
    assert len(column_rows) == len(SCHEMA)
    assert [row[2] for row in column_rows] == ["id", "email", "age"]
    assert [row[1] for row in column_rows] == [0, 1, 2], "positions are recorded"


def test_ids_are_assigned_in_order_from_the_reserved_boundary(catalog: Catalog):
    first = catalog.create_table("a", SCHEMA)
    second = catalog.create_table("b", SCHEMA)
    assert first.table_id == FIRST_USER_OBJECT_ID
    assert second.table_id == FIRST_USER_OBJECT_ID + 1
    # Reserved ids must stay out of reach.
    assert first.table_id > COLUMNS_TABLE_ID


def test_a_duplicate_name_is_refused_case_insensitively(catalog: Catalog):
    catalog.create_table("users", SCHEMA)
    for name in ("users", "USERS", "Users"):
        with pytest.raises(CatalogError, match="already exists"):
            catalog.create_table(name, SCHEMA)


@pytest.mark.parametrize("name", ["chendb_tables", "chendb_anything", "CHENDB_x"])
def test_reserved_names_are_refused(catalog: Catalog, name: str):
    with pytest.raises(CatalogError, match="reserved"):
        catalog.create_table(name, SCHEMA)


def test_is_system_table_matches_on_the_prefix_not_a_known_list():
    # So a future system table is protected the moment it is named, and a user
    # cannot squat on the name first.
    assert is_system_table("chendb_tables")
    assert is_system_table("chendb_indexes")  # does not exist yet
    assert is_system_table("CHENDB_TABLES")
    assert not is_system_table("users")
    assert not is_system_table("my_chendb_table")


# -- looking up ------------------------------------------------------------


def test_lookup_rebuilds_the_schema_from_the_column_rows(catalog: Catalog):
    catalog.create_table("users", SCHEMA)
    catalog.invalidate()

    info = catalog.get_table("users")
    assert info is not None
    assert info.schema == SCHEMA
    assert info.schema is not SCHEMA, "rebuilt, not the object we passed in"


def test_every_column_attribute_survives_the_round_trip(catalog: Catalog):
    schema = Schema.of(
        Column("pk", DataType.INTEGER, nullable=False, primary_key=True),
        Column("req", DataType.TEXT, nullable=False),
        Column("opt", DataType.FLOAT, nullable=True),
        Column("flag", DataType.BOOLEAN, nullable=True),
    )
    catalog.create_table("t", schema)
    catalog.invalidate()
    assert catalog.get_table("t").schema == schema  # type: ignore[union-attr]


def test_column_order_comes_from_position_not_physical_order(catalog: Catalog):
    """A heap scan returns physical order, which is not column order.

    With enough columns to span pages, and after a delete that lets a slot be
    reused, physical order genuinely diverges — so `position` is what makes the
    rebuild correct rather than lucky.
    """
    wide = Schema(
        tuple(
            Column(f"c{index}", DataType.INTEGER, nullable=index > 0)
            for index in range(20)
        )
    )
    catalog.create_table("wide", wide)
    catalog.invalidate()
    assert catalog.get_table("wide").schema.column_names == wide.column_names  # type: ignore[union-attr]


def test_an_absent_table_is_none_not_an_error(catalog: Catalog):
    assert catalog.get_table("nope") is None


def test_require_table_lists_what_does_exist(catalog: Catalog):
    catalog.create_table("users", SCHEMA)
    catalog.create_table("orders", SCHEMA)
    with pytest.raises(CatalogError, match="no table named 'nope'") as info:
        catalog.require_table("nope")
    assert "users" in str(info.value)
    assert "orders" in str(info.value)


def test_the_declared_case_is_kept_even_though_lookup_ignores_it(catalog: Catalog):
    catalog.create_table("MyTable", SCHEMA)
    assert catalog.require_table("mytable").name == "MyTable"


def test_listing_sorts_user_tables_and_hides_system_ones_by_default(catalog: Catalog):
    for name in ("zebra", "apple", "mango"):
        catalog.create_table(name, SCHEMA)

    assert [info.name for info in catalog.list_tables()] == ["apple", "mango", "zebra"]

    everything = [info.name for info in catalog.list_tables(include_system=True)]
    assert everything[:3] == ["apple", "mango", "zebra"]
    assert set(everything[3:]) == {
        TABLES_TABLE_NAME,
        COLUMNS_TABLE_NAME,
        INDEXES_TABLE_NAME,
    }


def test_a_column_row_with_no_table_row_is_corruption(catalog: Catalog):
    # Reaching for a table_id that was never created must not silently produce
    # an empty schema.
    with pytest.raises(CorruptDatabaseError, match="no columns"):
        catalog._load_schema(9999)


# -- caching ---------------------------------------------------------------


def test_a_lookup_miss_costs_scans_and_a_hit_costs_none(catalog: Catalog):
    catalog.create_table("users", SCHEMA)
    catalog.invalidate()

    scans_before = catalog.stats.scans
    catalog.get_table("users")
    assert catalog.stats.scans > scans_before, "a miss scans both system tables"

    scans_after_miss = catalog.stats.scans
    for _ in range(50):
        catalog.get_table("users")
    assert catalog.stats.scans == scans_after_miss
    assert catalog.stats.cache_hits == 50


def test_hit_rate_reports_the_cache_being_worth_it(catalog: Catalog):
    catalog.create_table("users", SCHEMA)
    for _ in range(9):
        catalog.get_table("users")
    assert catalog.stats.hit_rate > 0.8


def test_a_freshly_created_table_is_cached_without_a_scan(catalog: Catalog):
    scans_before = catalog.stats.scans
    catalog.create_table("users", SCHEMA)
    # create_table checks for a duplicate, which scans once; the lookup right
    # after must not scan again.
    scans_after_create = catalog.stats.scans
    catalog.get_table("users")
    assert catalog.stats.scans == scans_after_create
    assert catalog.stats.scans > scans_before


def test_stats_start_empty(catalog: Catalog):
    assert catalog.stats.hit_rate == 0.0, "no division by zero on a fresh catalog"


# -- heaps -----------------------------------------------------------------


def test_heap_for_returns_a_usable_heap(catalog: Catalog):
    catalog.create_table("users", SCHEMA)
    heap = catalog.heap_for("users")
    record_id = heap.insert(b"payload")
    assert heap.get(record_id) == b"payload"


def test_the_same_heap_object_is_reused(catalog: Catalog):
    catalog.create_table("users", SCHEMA)
    assert catalog.heap_for("users") is catalog.heap_for("users")


def test_extending_a_heap_updates_its_catalog_row(catalog: Catalog):
    """If last_page went stale, a reopened database would append mid-chain."""
    catalog.create_table("users", SCHEMA)
    heap = catalog.heap_for("users")
    first_page = heap.first_page_id

    payload = b"x" * 60
    while heap.last_page_id == first_page:
        heap.insert(payload)

    rows = [
        decode_record(TABLES_TABLE_SCHEMA, payload)
        for _, payload in catalog.tables_heap.scan()
    ]
    row = next(row for row in rows if row[1] == "users")
    assert row[3] == heap.last_page_id, "chendb_tables must follow the heap"
    assert catalog.require_table("users").last_page == heap.last_page_id


def test_heap_for_an_unknown_table_raises(catalog: Catalog):
    with pytest.raises(CatalogError, match="no table named"):
        catalog.heap_for("nope")


# -- diagnostics -----------------------------------------------------------


def test_lookups_are_reported_with_their_cache_outcome(pager: Pager):
    sink = RingBufferSink()
    instance = Catalog(pager, tracer=Tracer(sink, TraceLevel.STORAGE))
    instance.bootstrap()
    instance.create_table("users", SCHEMA)
    instance.invalidate()

    instance.get_table("users")  # miss
    instance.get_table("users")  # hit
    instance.get_table("nope")  # absent

    lookups = [
        item.event for item in sink.snapshot() if item.event_type == "CatalogLookupEvent"
    ]
    outcomes = [(event.name, event.found, event.from_cache) for event in lookups]
    assert ("users", True, False) in outcomes
    assert ("users", True, True) in outcomes
    assert ("nope", False, False) in outcomes


def test_creating_a_table_is_reported(pager: Pager):
    sink = RingBufferSink()
    instance = Catalog(pager, tracer=Tracer(sink, TraceLevel.SUMMARY))
    instance.bootstrap()
    instance.create_table("users", SCHEMA)

    created = [
        item.event for item in sink.snapshot() if item.event_type == "TableCreatedEvent"
    ]
    assert len(created) == 1
    assert created[0].table_name == "users"
    assert created[0].column_count == len(SCHEMA)


# -- persistence -----------------------------------------------------------


def test_the_catalog_reopens_from_the_meta_pointers(db_path):
    with Pager(db_path, page_size=256) as pager:
        instance = Catalog(pager)
        instance.bootstrap()
        instance.create_table("users", SCHEMA)
        instance.create_table("orders", SCHEMA)

    with Pager(db_path) as pager:
        reopened = Catalog(pager)
        assert reopened.initialised
        assert [info.name for info in reopened.list_tables()] == ["orders", "users"]
        assert reopened.require_table("users").schema == SCHEMA


def test_many_tables_spill_the_catalog_across_pages(db_path):
    # 40 tables x 3 columns is 120 chendb_columns rows, well past one 256-byte
    # page — so the catalog's own heaps have to chain correctly too.
    with Pager(db_path, page_size=256) as pager:
        instance = Catalog(pager)
        instance.bootstrap()
        for index in range(40):
            instance.create_table(f"t{index:02d}", SCHEMA)
        assert instance.tables_heap.page_count() > 1
        assert instance.columns_heap.page_count() > 1

    with Pager(db_path) as pager:
        reopened = Catalog(pager)
        assert len(reopened.list_tables()) == 40
        assert reopened.require_table("t39").schema == SCHEMA
