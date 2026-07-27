#!/usr/bin/env python3
"""A narrated tour of the Milestone 7 buffer pool.

    python examples/milestone7_buffer_pool.py

Five things: what a page read used to cost and what it costs now, why the
answer turned out not to be about I/O at all, what write-back saves, how LRU
behaves when the working set fits and how badly it behaves when it does not,
and what all of that did to the planner's choices.
"""

from __future__ import annotations

import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.executor.engine import execute_script
from engine.optimizer.cost import PAGE_HIT_COST, PAGE_MISS_COST, distinct_pages_touched
from engine.storage.page import Page

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("bucket", DataType.INTEGER, nullable=False),
    Column("email", DataType.TEXT, nullable=False),
)

ROW_COUNT = 6_000
PAGE_SIZE = 4096


def rule(title: str) -> None:
    print(f"\n{'-' * 78}\n{title}\n")


def per_call_ns(fn, calls: int, repeats: int = 3) -> float:
    runs = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        fn()
        runs.append((time.perf_counter_ns() - started) / calls)
    return min(runs)


def main() -> int:
    print("ChenDB Milestone 7 - the buffer pool")

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "shop.chendb"
        with Database.open(path, page_size=PAGE_SIZE) as db:
            db.create_table("users", SCHEMA)
            db.insert_many(
                "users",
                [(n, n % 1000, f"u{n:06d}@example.com") for n in range(ROW_COUNT)],
            )
            db.sync()
            pager, pool = db.pager, db.pager.buffer_pool
            pages = list(db.heap_for("users").page_ids())

            # -- 1. what a read costs --------------------------------------
            rule("1. What a page read costs, hit against miss")

            page_id = pages[len(pages) // 2]
            pager.read_page(page_id)
            hit = per_call_ns(
                lambda: [pager.read_page(page_id) for _ in range(20_000)], 20_000
            )

            def cold() -> None:
                for _ in range(2_000):
                    pool.invalidate(page_id)
                    pager.read_page(page_id)

            miss = per_call_ns(cold, 2_000)
            print(f"   pool HIT       {hit:8.0f} ns")
            print(f"   pool MISS      {miss:8.0f} ns   ({miss / hit:.1f}x a hit)")
            print("\n   The cost model uses exactly this ratio:")
            print(f"     PAGE_HIT_COST  {PAGE_HIT_COST}")
            print(f"     PAGE_MISS_COST {PAGE_MISS_COST}")

            # -- 2. where the cost actually was ----------------------------
            rule("2. Why the pool nearly did not help")

            raw = pool.fetch(page_id)
            crc = per_call_ns(lambda: [zlib.crc32(raw) for _ in range(20_000)], 20_000)
            build = per_call_ns(
                lambda: [
                    Page.from_bytes(
                        page_id, raw, PAGE_SIZE, verify_checksum=False, validate=False
                    )
                    for _ in range(20_000)
                ],
                20_000,
            )
            page = Page.from_bytes(
                page_id, raw, PAGE_SIZE, verify_checksum=False, validate=False
            )
            walk = per_call_ns(lambda: [page.validate() for _ in range(2_000)], 2_000)
            read = per_call_ns(
                lambda: [pager._read_from_disk(page_id) for _ in range(20_000)],
                20_000,
            )
            print(f"   pread, OS cached          {read:8.0f} ns")
            print(f"   CRC32 over the page       {crc:8.0f} ns")
            print(f"   build a Page object       {build:8.0f} ns")
            print(
                f"   validate() - every slot   {walk:8.0f} ns   <-- {walk / crc:.0f}x the checksum"
            )
            print("\n   The first version of this milestone kept validate() on the read")
            print("   path and the pool was almost worthless: it removed the syscall")
            print("   and left the expensive half. validate() is now split - the O(1)")
            print("   header checks run on every read, the slot walk is explicit and")
            print("   belongs to the page inspector. PostgreSQL draws the same line.")

            # -- 3. write-back ---------------------------------------------
            rule("3. What write-back saves")

            before = (pager.stats.page_writes, pager.stats.physical_writes)
            db.insert_many(
                "users",
                [(100_000 + n, n % 10, f"late{n}@x.com") for n in range(1_000)],
            )
            logical = pager.stats.page_writes - before[0]
            physical = pager.stats.physical_writes - before[1]
            print(f"   1,000 inserts:  {logical} logical writes, {physical} syscalls")
            print(
                f"   {pool.stats.writes_absorbed:,} writes absorbed in total "
                f"({100 * (1 - physical / max(logical, 1)):.0f}% of this batch)"
            )
            print("\n   A page written two hundred times reaches the disk once. Before")
            print("   Milestone 7 that was two hundred syscalls - which is why an index")
            print("   build was so slow.")
            db.sync()
            print(f"   after sync(): {pool.dirty_pages} dirty frames - the durability")
            print("   contract is unchanged, the crash window between syncs is wider.")

            # -- 4. the policy ---------------------------------------------
            rule("4. LRU: when it works, and when it is the worst choice")

            # A deliberately tiny pool, because the default holds this whole
            # database and nothing would ever be evicted. Same file, same rows.
            small_frames = 16
            with Database.open(
                path, page_size=PAGE_SIZE, buffer_pool_frames=small_frames
            ) as tiny:
                tiny_pager = tiny.pager
                tiny_pool = tiny_pager.buffer_pool
                tiny_pages = list(tiny.heap_for("users").page_ids())
                print(
                    f"   the table is {len(tiny_pages)} pages; this pool holds "
                    f"{tiny_pool.capacity}"
                )

                def hit_rate_for(fetch_pages: list[int], passes: int = 4) -> float:
                    tiny_pool.clear()
                    base = tiny_pool.stats.hits, tiny_pool.stats.lookups
                    for _ in range(passes):
                        for pid in fetch_pages:
                            tiny_pager.read_page(pid)
                    hits = tiny_pool.stats.hits - base[0]
                    lookups = tiny_pool.stats.lookups - base[1]
                    return hits / lookups if lookups else 0.0

                fits = tiny_pages[: small_frames - 2]
                print(f"\n   working set of {len(fits)} pages (fits):")
                print(
                    f"     hit rate {hit_rate_for(fits):.1%}"
                    f"  - loaded once, then served from memory"
                )
                print(f"\n   scanning all {len(tiny_pages)} pages (does not fit):")
                print(
                    f"     hit rate {hit_rate_for(tiny_pages):.1%}  <-- sequential flooding"
                )
            print("\n   Every page a scan loads is evicted by the pages behind it before")
            print("   the next pass reaches it. LRU is the worst possible policy here,")
            print("   and it is why PostgreSQL confines large scans to a small ring")
            print("   buffer rather than letting them run through shared_buffers.")

            # -- 5. what the planner does about it -------------------------
            rule("5. What that did to the planner")

            db.create_index("users_bucket", "users", "bucket")
            db.analyze("users")
            stats = db.statistics.for_table("users")
            print(f"   {'matching rows':>14}{'distinct pages':>16}{'hits':>10}   estimate")
            for matching in (1, 50, 500, 5000):
                distinct = distinct_pages_touched(matching, stats.page_count)
                print(
                    f"   {matching:>14}{distinct:>16.0f}{matching - distinct:>10.0f}"
                    f"   {'mostly misses' if distinct > matching * 0.8 else 'mostly hits'}"
                )
            print("\n   That is the Cardenas occupancy formula, and without it the pool")
            print("   would be invisible to the planner: every fetch charged as a miss,")
            print("   and a wide index scan costed three times what it actually costs.")

            print(f"\n   {'predicate':<34}{'chose':>14}")
            for cutoff in (5, 100, 400, 900):
                sql = f"SELECT id FROM users WHERE bucket < {cutoff}"
                result = execute_script(sql, db)[0]
                chosen = next(a for a in result.planned.alternatives if a.chosen)
                path = "index scan" if "Index" in chosen.access_path else "seq scan"
                print(f"   bucket < {cutoff:<25}{path:>14}")
            print("\n   The crossover MOVED. `bucket < 200` was a sequential scan in")
            print("   Milestone 6 and is an index scan now, because the pool made the")
            print("   heap fetches four times cheaper. The constants had to be")
            print("   re-measured for the planner to notice.")

            # -- events ------------------------------------------------------
            rule("6. What the pool reported")

            sink = RingBufferSink(capacity=100_000)
            tracer = Tracer(sink, TraceLevel.STORAGE)
            db._tracer = tracer
            pager._tracer = tracer
            pool._tracer = tracer
            pool.clear()
            db.rows("users")  # cold: every page a miss
            db.rows("users")  # warm: the table fits, so every page a hit

            counts: dict[str, int] = {}
            tracer.level = TraceLevel.VERBOSE  # so hits are reported too
            db.rows("users")
            for item in sink.snapshot():
                if item.event_type == "BufferPoolEvent":
                    counts[item.event.action] = counts.get(item.event.action, 0) + 1
            for action, count in sorted(counts.items(), key=lambda e: -e[1]):
                print(f"   {action:<12}{count:>8}")
            sources: dict[str, int] = {}
            for item in sink.snapshot():
                if item.event_type == "PageReadEvent":
                    sources[item.event.source] = sources.get(item.event.source, 0) + 1
            print("\n   PageReadEvent.source, finally non-constant:")
            for source, count in sorted(sources.items()):
                print(f"   {source:<12}{count:>8}")
            print("\n   That field has been in the schema since Milestone 1, always")
            print("   reading 'disk', so this milestone needed no consumer to change.")
            print("   `hit` only shows once the level is raised to VERBOSE: one event")
            print("   per cached read would drown everything else in the stream.")

    print("\n" + "-" * 78)
    print("Try it in the browser: python -m engine.server, then the Buffer pool tab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
