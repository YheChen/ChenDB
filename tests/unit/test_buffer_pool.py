"""The buffer pool.

Two properties matter and everything here tests one of them:

* **correctness**: a page read back is the page that was written, whether it
  came from a frame, from the disk, or from a frame that was evicted and read
  again. The pool must be invisible except in the timings.
* **the policy actually working**: a hit is served without touching the disk,
  a repeated write reaches the disk once, and eviction picks the least recently
  used page rather than an arbitrary one.

The pool is given its storage as two callables, so most of this runs against a
dictionary. That is not a shortcut: it means the tests can assert *exactly*
which pages reached the disk and when, which is the whole point and is
impossible to see through a real file.
"""

from __future__ import annotations

import pytest

from engine import Column, Database, DataType, Schema
from engine.storage.buffer import (
    DEFAULT_POOL_FRAMES,
    MIN_POOL_FRAMES,
    BufferPool,
)

PAGE_SIZE = 64


class FakeDisk:
    """A dict pretending to be a file, recording every access."""

    def __init__(self, page_size: int = PAGE_SIZE) -> None:
        self.page_size = page_size
        self.pages: dict[int, bytes] = {}
        self.reads: list[int] = []
        self.writes: list[int] = []

    def read(self, page_id: int) -> bytes:
        self.reads.append(page_id)
        return self.pages.get(page_id, bytes(self.page_size))

    def write(self, page_id: int, raw: bytes) -> None:
        self.writes.append(page_id)
        self.pages[page_id] = bytes(raw)


def content(page_id: int, marker: int = 0) -> bytes:
    """A page image that identifies itself, so a mix-up is obvious."""
    return (f"page-{page_id}-{marker}".encode()).ljust(PAGE_SIZE, b".")


@pytest.fixture
def disk() -> FakeDisk:
    return FakeDisk()


def make_pool(disk: FakeDisk, capacity: int = 4) -> BufferPool:
    return BufferPool(
        page_size=disk.page_size,
        capacity=capacity,
        read_through=disk.read,
        write_through=disk.write,
    )


# -- correctness ------------------------------------------------------------


def test_a_page_reads_back_as_it_was_written(disk: FakeDisk):
    pool = make_pool(disk)
    pool.store(1, content(1))
    assert pool.fetch(1) == content(1)


def test_a_write_survives_eviction_and_a_reread(disk: FakeDisk):
    # The one thing that must never break: write-back means the bytes are only
    # in memory for a while, and an evicted page has to reach the disk first.
    pool = make_pool(disk, capacity=2)
    pool.store(1, content(1))
    pool.fetch(2)
    pool.fetch(3)  # evicts page 1, which is dirty
    assert pool.fetch(1) == content(1)


def test_the_pool_is_invisible_to_a_reader(disk: FakeDisk):
    pool = make_pool(disk, capacity=2)
    for page_id in range(6):
        pool.store(page_id, content(page_id))
    for page_id in range(6):
        assert pool.fetch(page_id) == content(page_id), page_id


def test_a_fetch_returns_a_copy_not_the_frame(disk: FakeDisk):
    # This is what removes the need for pin counts: a caller's bytes cannot be
    # changed underneath it, so eviction can never invalidate one.
    pool = make_pool(disk)
    pool.store(1, content(1))
    held = pool.fetch(1)
    pool.store(1, content(1, marker=2))
    assert held == content(1), "the earlier caller's copy must not have changed"
    assert pool.fetch(1) == content(1, marker=2)


def test_a_wrong_sized_page_is_refused(disk: FakeDisk):
    pool = make_pool(disk)
    with pytest.raises(ValueError, match="refusing to buffer"):
        pool.store(1, b"too short")


def test_a_pool_too_small_to_work_is_refused():
    with pytest.raises(ValueError, match=f"at least {MIN_POOL_FRAMES}"):
        BufferPool(
            page_size=PAGE_SIZE,
            capacity=1,
            read_through=lambda _: b"",
            write_through=lambda _a, _b: None,
        )


# -- hits and misses --------------------------------------------------------


def test_a_second_read_does_not_touch_the_disk(disk: FakeDisk):
    pool = make_pool(disk)
    pool.fetch(1)
    pool.fetch(1)
    pool.fetch(1)
    assert disk.reads == [1], "one physical read for three logical ones"
    assert pool.stats.hits == 2
    assert pool.stats.misses == 1


def test_the_hit_rate_reports_what_happened(disk: FakeDisk):
    pool = make_pool(disk)
    pool.fetch(1)
    for _ in range(9):
        pool.fetch(1)
    assert pool.stats.hit_rate == pytest.approx(0.9)


def test_a_fresh_pool_reports_no_hit_rate_rather_than_dividing_by_zero(disk: FakeDisk):
    assert make_pool(disk).stats.hit_rate == 0.0


def test_a_write_to_a_missing_page_does_not_read_it_first(disk: FakeDisk):
    # There is nothing to learn from the old bytes: the caller has a whole page.
    pool = make_pool(disk)
    pool.store(1, content(1))
    assert disk.reads == []


# -- write-back -------------------------------------------------------------


def test_a_write_does_not_reach_the_disk(disk: FakeDisk):
    pool = make_pool(disk)
    pool.store(1, content(1))
    assert disk.writes == [], "the whole point of write-back"


def test_repeated_writes_reach_the_disk_once(disk: FakeDisk):
    # The largest single win: an index build touches one leaf hundreds of times.
    pool = make_pool(disk)
    for marker in range(100):
        pool.store(1, content(1, marker))
    pool.flush()
    assert disk.writes == [1]
    assert disk.pages[1] == content(1, 99)
    assert pool.stats.writes_absorbed == 99


def test_flush_writes_every_dirty_frame_and_no_clean_one(disk: FakeDisk):
    pool = make_pool(disk, capacity=4)
    pool.store(1, content(1))
    pool.store(2, content(2))
    pool.fetch(3)  # clean
    written = pool.flush()
    assert written == 2
    assert sorted(disk.writes) == [1, 2]


def test_flushing_twice_writes_nothing_the_second_time(disk: FakeDisk):
    pool = make_pool(disk)
    pool.store(1, content(1))
    pool.flush()
    disk.writes.clear()
    assert pool.flush() == 0
    assert disk.writes == []


def test_a_clean_eviction_writes_nothing(disk: FakeDisk):
    pool = make_pool(disk, capacity=2)
    pool.fetch(1)
    pool.fetch(2)
    pool.fetch(3)
    assert disk.writes == []
    assert pool.stats.evictions == 1
    assert pool.stats.dirty_evictions == 0


def test_a_dirty_eviction_writes_the_page_back(disk: FakeDisk):
    pool = make_pool(disk, capacity=2)
    pool.store(1, content(1))
    pool.fetch(2)
    pool.fetch(3)
    assert disk.writes == [1]
    assert pool.stats.dirty_evictions == 1


# -- eviction policy --------------------------------------------------------


def test_eviction_picks_the_least_recently_used(disk: FakeDisk):
    pool = make_pool(disk, capacity=3)
    pool.fetch(1)
    pool.fetch(2)
    pool.fetch(3)
    pool.fetch(1)  # 2 is now the oldest
    pool.fetch(4)  # evicts 2

    assert pool.contains(1) and pool.contains(3) and pool.contains(4)
    assert not pool.contains(2)


def test_a_write_counts_as_a_use(disk: FakeDisk):
    pool = make_pool(disk, capacity=3)
    pool.fetch(1)
    pool.fetch(2)
    pool.fetch(3)
    pool.store(1, content(1))  # 1 is now newest, 2 the oldest
    pool.fetch(4)
    assert pool.contains(1)
    assert not pool.contains(2)


def test_a_scan_larger_than_the_pool_hits_nothing(disk: FakeDisk):
    # Sequential flooding: every page is touched once, evicting everything
    # useful behind it. PostgreSQL confines large scans to a ring buffer for
    # exactly this; ChenDB does not, and the cost model charges a scan as
    # all-misses because of it.
    pool = make_pool(disk, capacity=4)
    for _ in range(3):
        for page_id in range(20):
            pool.fetch(page_id)
    assert pool.stats.hits == 0, "LRU is the worst possible policy for this"
    assert pool.stats.misses == 60


def test_a_working_set_that_fits_hits_almost_always(disk: FakeDisk):
    pool = make_pool(disk, capacity=8)
    for _ in range(50):
        for page_id in range(4):
            pool.fetch(page_id)
    assert pool.stats.misses == 4, "one miss per page, ever"
    assert pool.stats.hit_rate > 0.97


# -- invalidation -----------------------------------------------------------


def test_invalidating_drops_a_page_without_writing_it(disk: FakeDisk):
    # For a page about to be overwritten wholesale: a fresh allocation, or one
    # returned to the free list. Writing back superseded bytes is pure waste.
    pool = make_pool(disk)
    pool.store(1, content(1))
    pool.invalidate(1)
    assert disk.writes == []
    assert not pool.contains(1)


def test_invalidating_frees_the_frame_for_reuse(disk: FakeDisk):
    pool = make_pool(disk, capacity=2)
    pool.fetch(1)
    pool.fetch(2)
    pool.invalidate(1)
    pool.fetch(3)
    assert pool.stats.evictions == 0, "a free frame was available"


def test_invalidating_a_page_that_is_not_resident_is_harmless(disk: FakeDisk):
    make_pool(disk).invalidate(99)


def test_clear_flushes_before_dropping_everything(disk: FakeDisk):
    pool = make_pool(disk)
    pool.store(1, content(1))
    pool.clear()
    assert disk.writes == [1]
    assert pool.resident_pages == 0


# -- the snapshot the API serves --------------------------------------------


def test_the_snapshot_describes_every_frame(disk: FakeDisk):
    pool = make_pool(disk, capacity=4)
    pool.fetch(1)
    pool.store(2, content(2))
    snapshot = pool.snapshot()

    assert snapshot.capacity == 4
    assert len(snapshot.frames) == 4
    assert snapshot.resident == 2
    assert snapshot.dirty == 1

    by_page = {frame.page_id: frame for frame in snapshot.frames}
    assert by_page[1].dirty is False
    assert by_page[2].dirty is True
    assert by_page[None].page_id is None, "free frames are reported too"


def test_recency_orders_the_frames_most_recent_first(disk: FakeDisk):
    pool = make_pool(disk, capacity=4)
    pool.fetch(1)
    pool.fetch(2)
    pool.fetch(3)
    pool.fetch(1)  # 1 is newest again
    recency = {
        frame.page_id: frame.recency
        for frame in pool.snapshot().frames
        if frame.page_id is not None
    }
    assert recency[1] == 0
    assert recency[3] == 1
    assert recency[2] == 2


def test_a_free_frame_has_no_recency(disk: FakeDisk):
    pool = make_pool(disk, capacity=4)
    pool.fetch(1)
    free = [frame for frame in pool.snapshot().frames if frame.page_id is None]
    assert free and all(frame.recency == -1 for frame in free)


def test_the_snapshot_does_not_change_when_the_pool_does(disk: FakeDisk):
    # Frozen dataclasses, so the API can serialise one after releasing the lock.
    pool = make_pool(disk, capacity=4)
    pool.fetch(1)
    snapshot = pool.snapshot()
    pool.fetch(2)
    assert snapshot.resident == 1


# -- through a real database ------------------------------------------------


SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)


@pytest.fixture
def db(tmp_path):
    with Database.open(tmp_path / "pool.chendb", page_size=512) as handle:
        handle.create_table("t", SCHEMA)
        handle.insert_many("t", [(n, f"row{n:04d}") for n in range(400)])
        handle.sync()
        yield handle


def test_the_engine_reads_far_fewer_pages_than_it_asks_for(db: Database):
    before = db.stats.page_reads, db.stats.physical_reads
    for _ in range(5):
        db.rows("t")
    logical = db.stats.page_reads - before[0]
    physical = db.stats.physical_reads - before[1]
    assert logical > physical, "the pool must be doing something"
    assert db.stats.cache_hit_rate > 0.5


def test_the_second_scan_of_a_resident_table_touches_no_disk(db: Database):
    db.rows("t")
    before = db.stats.physical_reads
    db.rows("t")
    assert db.stats.physical_reads == before, "everything was already resident"


def test_inserts_are_written_once_not_once_per_row(db: Database):
    # Before Milestone 7 this was one syscall per row.
    before = db.stats.physical_writes
    db.insert_many("t", [(1000 + n, f"more{n}") for n in range(200)])
    during = db.stats.physical_writes - before
    db.sync()
    total = db.stats.physical_writes - before
    assert during < 200, "write-back must absorb most of them"
    assert total < 200


def test_sync_makes_everything_durable(db: Database, tmp_path):
    db.insert_many("t", [(2000 + n, f"sync{n}") for n in range(50)])
    db.sync()
    path = db.path
    db.close()
    with Database.open(path, page_size=512) as reopened:
        assert reopened.count("t") == 450


def test_closing_flushes_even_without_an_explicit_sync(db: Database):
    path = db.path
    db.insert_many("t", [(3000 + n, f"close{n}") for n in range(50)])
    db.close()
    with Database.open(path, page_size=512) as reopened:
        assert reopened.count("t") == 450


def test_a_page_read_reports_where_it_came_from(db: Database):
    from engine.diagnostics import RingBufferSink, TraceLevel, Tracer

    sink = RingBufferSink(capacity=10_000)
    db._tracer = Tracer(sink, TraceLevel.STORAGE)
    db.pager._tracer = db._tracer
    db.pager.buffer_pool._tracer = db._tracer

    db.pager.buffer_pool.clear()
    db.rows("t")
    db.rows("t")
    sources = {
        item.event.source for item in sink.snapshot() if item.event_type == "PageReadEvent"
    }
    assert sources == {"disk", "buffer_pool"}, (
        "the source field was in the schema from Milestone 1 for this moment"
    )


def test_the_default_pool_is_big_enough_to_be_useful(db: Database):
    assert db.pager.buffer_pool.capacity == DEFAULT_POOL_FRAMES
    assert DEFAULT_POOL_FRAMES >= 16
