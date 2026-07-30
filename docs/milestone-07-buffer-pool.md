# Milestone 7: the buffer pool

Milestones 1–6 gave the pager no memory. Every read was a `pread` plus a CRC32
plus a walk of the slot directory; every write was a `write`. This milestone
puts a cache between the engine and the file, and the interesting part is that
**the win had almost nothing to do with I/O**.

```
 read_page, resident (hit)     661 ns
 read_page, not resident       1822 ns   2.75x
```

But getting there meant finding out where the time actually went, and it was not
where the plan said it would be.

---

## What was built

```
engine/storage/buffer.py    BufferPool: frames, write-back, LRU, a snapshot
```

| | |
|---|---|
| **Pool** | fixed frames, copy-in/copy-out, write-back, exact LRU |
| **Pager** | all page I/O routed through it; logical stats split from physical |
| **Page** | `validate()` split into O(1) header checks and the O(slots) walk |
| **Cost model** | `PAGE_HIT_COST` / `PAGE_MISS_COST` replace `PAGE_COST` / `RANDOM_PAGE_COST`, plus hit estimation |
| **Diagnostics** | `BufferPoolEvent`; `PageReadEvent.source` finally non-constant |
| **API** | `GET /databases/{db}/buffer-pool`, the frame grid and the counters |
| **Visualizer** | Buffer pool workspace: counters, workloads, live frame grid |

---

## The finding: it was never the syscall

The first working version routed reads through the pool and changed almost
nothing. An index scan over 14,000 rows went from 307 ms to 283 ms, an 8%
improvement for a cache that was serving nearly every read from memory. That
made no sense until the read path was measured a piece at a time:

```
 pread, OS cached                303 ns
 CRC32 over 4 KiB                103 ns
 build a Page object             249 ns
 pool.fetch (a 4 KiB memcpy)     228 ns
 validate(): walks every slot 13 290 ns   ← 130x the checksum
```

`Page.validate()` walks the slot directory doing one `struct.unpack_from` per
slot. A full 4 KiB page holds about a hundred rows, so **every logical read cost
thirteen microseconds of pure bookkeeping**, dwarfing the syscall the pool had
just eliminated.

Two changes followed, and together they are what made the milestone worth
shipping:

1. **A page served from a frame is not re-verified.** Its checksum was checked
   when it was admitted, or its bytes came from `Page.to_bytes`, which
   recomputes the checksum. Re-checking proves nothing.
2. **`validate()` is off the read path entirely.** It is now split: the O(1)
   header checks (`validate_header`) run on every disk read, and the slot walk is
   explicit, called by the page inspector, by `BPlusTree.verify`, and by tests.

PostgreSQL draws exactly this line: it verifies the checksum and a few header
fields on read, and never walks the line pointer array to prove it is
self-consistent. A page that passes its checksum but has bad slots is an engine
bug, not media corruption, and a per-read scan is a poor way to find one.

The index scan then went **307 ms → 86 ms**.

The cost of that decision is real and worth naming: in-memory corruption of a
frame goes undetected. It is the same bet PostgreSQL makes, `data_checksums`
protects the storage path, not RAM, and ECC memory is the answer to the other.

---

## Why there are no pin counts

A textbook buffer pool hands out a *pointer into the frame* and requires callers
to **pin** it: two callers must see each other's writes, and a frame must not be
reused while someone holds it. Getting the second wrong lets eviction hand a
frame to a new page while another caller is still writing into it, silent
corruption.

ChenDB copies **out of** the frame on read and **into** it on write. A caller's
`Page` is an independent object, so eviction can never invalidate one: if a page
is evicted while a caller holds a copy, the caller's later `write_page` simply
re-admits it. Correct by construction, and **no caller in the engine had to
change**. The heap, the B+ tree and the inspector all kept working untouched.

That is affordable because the copy is 228 ns against the 1,822 ns miss it
avoids. It is not equivalent to the shared design:

- two callers mutating the same page still lose one of the updates, exactly as
  before the pool existed. The database-level write lock prevents that, not the
  pool.
- a shared-frame pool needs pinning, and becomes the right design the moment
  there are concurrent readers, Milestone 10.

Pin counts that are always zero would be *ceremony*: a number in the UI that
never prevents anything. `BufferPoolEvent` therefore has no `pin_count`, despite
the field being named in the planned event schema, and the frame grid does not
show one. Saying why is more useful than displaying a fake.

---

## Write-back

`write_page` no longer reaches the operating system. It marks a frame dirty; the
bytes go out on eviction or on `sync`. A page written repeatedly reaches the disk
once, which is exactly the shape of every heap append and every index build:

```
 2,500-row insert:  3,169 logical writes  →  143 syscalls   (95% absorbed)
```

`sync()` flushes dirty frames *before* the `fsync`, so the contract callers have
relied on since Milestone 1 is unchanged: after `sync()` returns, everything
acknowledged is durable.

Between syncs, more data now lives only in memory. **The crash window is wider**
. A process killed after twenty unsynced inserts used to lose whatever the OS
had not flushed, and now loses whatever the pool has not evicted, which is likely
all of it. The recovery tests already asserted only that *synced* rows survive,
so they passed unchanged; that is the honest cost of write-back, and closing it
is what Milestone 9's write-ahead log is for.

---

## Eviction: exact LRU, and why nobody ships it

Residency is an `OrderedDict`, so eviction is `popitem(last=False)` and touching
a page is `move_to_end`, both O(1), no scan.

Real systems do not do this. True LRU performs a **write to shared state on every
read**, which under concurrency means a lock on the hottest path in the engine.
PostgreSQL uses a clock sweep: each frame has a usage counter, a read only
increments it, and the evictor walks a circular buffer decrementing until it
finds a zero. Approximate LRU, no lock on read. That is a concurrency
optimisation, and ChenDB has one writer, so exact LRU costs nothing here and is
easier to reason about.

The other reason to avoid plain LRU is **sequential flooding**, and it is stark:

```
 working set of 14 pages, pool holds 16   hit rate  75.0%
 scanning 75 pages,       pool holds 16   hit rate   0.0%
```

Zero. Every page a scan loads is evicted by the pages behind it before the next
pass reaches it, so the pool does strictly no good *and* has thrown away
everything useful that was in it. PostgreSQL confines large scans to a small ring
buffer for exactly this. ChenDB does not, and
`examples/milestone7_buffer_pool.py` shows it happening.

---

## Recalibrating the cost model

The handover for this milestone said recalibration was mandatory, not optional,
and it was right. The pool made index scans roughly four times cheaper and left
sequential scans untouched, so Milestone 6's constants immediately mis-weighted
one path against the other:

```
 before recalibration:  index paths ~5 µs/cost unit, seq scan ~19
```

A model that self-consistently over-costs one access path picks the wrong plan
while looking fine. Two changes fixed it.

**`RANDOM_PAGE_COST` is gone.** Milestone 6 predicted it would start to matter;
it did not. With the pool the axis that decides cost is **hit against miss**, not
sequential against random. There is no seek on an SSD, and the OS page cache was
already flattening the difference. `PAGE_HIT_COST = 0.36` and
`PAGE_MISS_COST = 1.0` replaced it, both measured.

**The planner now estimates hits.** `distinct_pages_touched` is the Cárdenas
occupancy formula, with rows spread over *P* pages, *N* random fetches land on
`P × (1 − (1 − 1/P)^N)` distinct pages. Everything else is a hit. Without it the
pool is invisible to the planner: every fetch charged as a miss, and a wide index
scan costed three times what it actually costs. PostgreSQL uses the
Mackert–Lohman refinement of the same idea.

The fit afterwards:

| Plan | Estimated | Measured | µs per unit |
|---|---:|---:|---:|
| index scan, 20 rows | 30 | 0.2 ms | 8.2 |
| index scan, 1 000 rows | 955 | 5.9 ms | 6.1 |
| index scan, 4 000 rows | 3 374 | 22.8 ms | 6.8 |
| index scan, 14 000 rows | 11 432 | 81.3 ms | 7.1 |
| sequential scan + filter | 11 703 | 77.3 ms | 6.6 |

Flat across both access paths again. **And the crossover moved**, which is the
whole point:

```
 predicate         rows    seq scan  index scan   M6 chose   M7 chooses
 bucket < 200      4000     58.3 ms     22.8 ms   seq        index   ✓
 bucket < 700     14000     77.3 ms     80.7 ms   seq        seq     ✓
```

`bucket < 200` was a sequential scan in Milestone 6 and is an index scan now,
because the pool made the heap fetches four times cheaper. The planner is right
in all five benchmark rows both before and after, because the constants were
re-measured rather than assumed.

The optimism in the estimate is deliberate and worth naming: it assumes a page
touched once stays resident, which the flooding numbers above show is false once
the working set exceeds the pool. Modelling that needs the pool size *and* the
access pattern, which is what `effective_cache_size` approximates in PostgreSQL.

---

## Logical against physical

`PagerStats` now counts both, and the distinction is new:

- `page_reads` / `page_writes`, **logical**: how many times the engine asked.
  Unchanged in meaning, so a test asserting "this operation reads pages" still
  measures what it always did.
- `physical_reads` / `physical_writes`, syscalls.

The gap is the pool working. `cache_hit_rate` is that gap as a fraction.

`PageReadEvent.source` has said `"disk"` since Milestone 1 and now says
`"buffer_pool"` on a hit. The field was put in the schema then precisely so this
milestone would need no consumer to change, and it did not.

---

## What the visualizer shows

A **Buffer pool** workspace, gated on `features.buffer_pool`.

The frame grid draws every frame, including the free ones, so the grid keeps a
fixed shape and a page appearing reads as a change rather than a reflow. Frames
are in *frame* order, not recency order, because a frame is a physical slot and
watching one slot's contents get replaced is the thing worth seeing. Recency is
shown by ringing the coldest resident frame: "which one goes next" is the
question the policy exists to answer.

Above it, the counters, including how much I/O never happened:

```
 reads    2,901 asked ·   0 on disk    100% avoided
 writes   3,169 asked · 143 on disk     95% avoided
```

And three workload buttons, because a cache's behaviour is only legible when you
can *cause* it. "Scan twice" on a table that fits shows the second pass hitting
everything; on a table that does not, the hit rate barely moves, which is far
more convincing to watch than to read.

Every button runs real SQL through the ordinary query endpoint. Nothing is
simulated.

---

## Try it

```bash
python examples/milestone7_buffer_pool.py
```

```bash
python benchmarks/index_vs_scan.py
```

---

## What Milestone 8 needs from this

- **Write-back and transactions interact.** A rollback has to undo changes that
  may exist only in a dirty frame, never having reached the disk. That is
  easier, not harder, but the undo log has to be ordered against the flush.
- **`sync` is the commit point today.** Milestone 8 makes commit a transaction
  boundary rather than a file operation, and Milestone 9's WAL is what lets a
  commit be durable without flushing every dirty page.
- **The pool must not flush an uncommitted page** once transactions exist, or a
  crash mid-transaction leaves half of it on disk. That is precisely what
  write-ahead logging solves, and why the WAL comes after the pool rather than
  before it.
