# Performance

## Where the time goes in Milestone 1

Every `Pager.read_page` is a real `read` syscall and every `write_page` a real
`write`. There is no cache. The numbers below are therefore dominated by I/O
count, which is exactly the point: Milestone 7 will change them dramatically
and the visualizer will show it happening.

| Operation | Page reads | Page writes | `fsync` |
|---|---|---|---|
| Insert onto the tail page | 1 | 1 | 0 |
| Insert that extends the chain | 1 | 3 (tail, new page, meta) | 0 |
| Point read by `RecordId` | 1 | 0 | 0 |
| Delete | 1 | 1 | 0 |
| Full scan of *p* pages | *p* | 0 | 0 |
| `count()` | *p* | 0 | 0 |
| `sync()` | 0 | 0 | 1 |

A scan of *n* rows at *r* rows per page costs ⌈n/r⌉ reads. With 256-byte pages
and ~30-byte rows, *r* ≈ 7: 300 rows is 43 reads. With 4096-byte pages, *r* ≈
125: 300 rows is 3 reads. Page size is a real tuning knob, and the visualizer
lets you pick it at creation so the difference is visible.

## Tracing overhead

Run it:

```bash
python benchmarks/trace_overhead.py
```

The benchmark inserts 2000 rows and scans them at each trace level, and
separately measures the guarded and unguarded emit patterns.

Measured on Python 3.14, Apple silicon, APFS on NVMe — 2000 rows, two full
scans, 100 point reads, 40 deletes:

| Level | Time | vs `OFF` | Events | Marginal cost |
|---|---|---|---|---|
| `OFF` | 0.0450 s | 1.00× | 0 | — |
| `SUMMARY` | 0.0450 s | 1.00× | 6 | below noise |
| `OPERATOR` | 0.0445 s | 0.99× | 6 | below noise |
| `STORAGE` | 0.0549 s | 1.22× | 6 395 | 1.55 µs/event |
| `VERBOSE` | 0.0546 s | 1.21× | 6 495 | 1.48 µs/event |

`OPERATOR` measuring marginally *faster* than `OFF` is noise, not a finding —
it emits the same six events as `SUMMARY`, and the difference is under 1%.
Operator events start existing in Milestone 3.

And the guard, with tracing off, over 200 000 call sites:

| Pattern | Per call |
|---|---|
| `if tracer.storage: tracer.emit(...)` | **8.5 ns** |
| `tracer.emit(...)` | **296.0 ns** |

**35× per call site.** Python evaluates arguments before the call, so the
unguarded form allocates a `PageReadEvent` and throws it away. That is the
entire reason every emit in the engine is written behind a flag, and why the
flags are plain cached booleans rather than an enum comparison.

Retention is capped at `CHENDB_TRACE_CAPACITY` (default 20 000) per database.
Beyond that the oldest events are dropped and counted — never silently.

## Indexes — Milestone 5

`benchmarks/index_vs_scan.py`, 20,000 rows, 4 KiB pages.

```
 predicate            rows    no index      index    pages (scan → index)
 ─────────────────  ──────  ──────────  ─────────    ────────────────────
 id = k (point)          1    60.0 ms    0.183 ms    233 →     4
 bucket < 1             20    57.9 ms      0.7 ms    233 →    22
 bucket < 10           200    58.6 ms      4.8 ms
 bucket < 50          1000    60.0 ms     22.7 ms    233 →  1009
 bucket < 200         4000    63.3 ms     92.9 ms    ← index now slower
 bucket < 700        10000    59.8 ms    225.2 ms    166 → 10076
```

A point lookup is **328× faster**; a predicate matching 70% of the table is
**3.8× slower**. Both come from the same fact: a sequential scan reads every page
exactly once whatever the predicate, and an index scan reads one heap page per
matching row — the *same* page repeatedly when matches share it, because there is
no buffer pool until Milestone 7.

The crossover is between 5% and 20% selectivity. Milestone 5's planner chooses by
rule and does not look, so it takes the index in every row of that table.
Milestone 6's cost model is what makes the last two rows come out as `SeqScan`.

Two more numbers worth having:

| | |
|---|---|
| `CREATE INDEX` over 20,000 rows | 3186 ms, 185 splits, height 3, 188 pages |
| Full ordered scan of the index | 35.8 ms for 20,000 entries, no sort |

The build cost is row-by-row insertion, O(n log n). Sorting first and packing
leaves in one pass — what a real system does — would be O(n) after the sort and
would leave no wasted space. The ordered scan is free ordering that nothing
exploits yet: there is no `ORDER BY`.

---

## The cost model — Milestone 6

The constants in `engine/optimizer/cost.py` are **measured for this engine**,
not copied from PostgreSQL, and the difference is the point. PostgreSQL's
defaults make CPU a hundred times cheaper than a page read; here it is about a
seventh, because a page read hits the OS cache while a row costs interpreted
Python.

```python
PAGE_COST          = 1.0    # pread + CRC32 over the page — the unit
RANDOM_PAGE_COST   = 1.0    # no buffer pool yet, so locality is nearly free
CPU_TUPLE_COST     = 0.15   # decode one record
CPU_PREDICATE_COST = 0.05   # evaluate a predicate on an already-decoded row
CPU_INDEX_COST     = 0.005  # compare one key inside a node — a memcmp
```

The fit, from `benchmarks/index_vs_scan.py`:

| Plan | Estimated | Measured | µs per unit |
|---|---:|---:|---:|
| index scan, 20 rows | 25 | 0.7 ms | 26.7 |
| index scan, 1 000 rows | 1 168 | 22.2 ms | 19.0 |
| index scan, 4 000 rows | 4 667 | 87.2 ms | 18.7 |
| index scan, 14 000 rows | 16 328 | 307.5 ms | 18.8 |
| sequential scan + filter | 4 303 | 81.3 ms | 18.9 |

Near-constant over a 650× range **and the same for both access paths**. The
second part is what matters — a model that is internally consistent but
mis-weights one path against the other picks the wrong plan while looking
calibrated. That was a real bug here: charging a full `CPU_TUPLE_COST` for
predicate evaluation double-counted the decode and over-costed a filtered
sequential scan by 45%.

The payoff, against the Milestone 5 numbers above:

```
 predicate            rows    seq scan  index scan   M5 chose    M6 chooses
 bucket < 200         4000     61.4 ms     90.8 ms   index ✗     seq   ✓
 bucket < 700        14000     80.8 ms    310.0 ms   index ✗     seq   ✓
```

`ANALYZE` costs one full scan: 3.2 s over 20,000 rows at 4 KiB pages, building
an exact distinct-value set per column. A real system samples — PostgreSQL reads
about 30,000 rows however large the table is — because a full scan to refresh an
*estimate* is absurd past a certain size.

Milestone 7's buffer pool makes `RANDOM_PAGE_COST` mean something and will move
the crossover. The µs-per-unit column above is the regression test: if it stops
being flat, the model needs recalibrating before the planner can be trusted.

---

## The buffer pool — Milestone 7

Measured on the read path, 4 KiB pages:

```
 pread, OS cached                303 ns
 CRC32 over the page             103 ns
 build a Page object             249 ns
 pool.fetch (a 4 KiB memcpy)     228 ns
 validate() — walks every slot 13 290 ns   ← 130x the checksum
```

`Page.validate()` was the entire cost of a page read, and it is bookkeeping:
one `struct.unpack_from` per slot, about a hundred on a full page. Splitting it —
O(1) header checks on the read path, the slot walk explicit — plus skipping
verification for a page already resident is what made the pool worth having:

```
 read_page, resident (hit)     661 ns
 read_page, not resident      1822 ns   2.75x
 index scan, 14 000 rows      307 ms → 86 ms
```

Write-back:

```
 2,500-row insert:  3,169 logical writes → 143 syscalls   (95% absorbed)
```

Sequential flooding, with a 16-frame pool over a 75-page table:

```
 working set of 14 pages (fits)      hit rate  75.0%
 scanning all 75 pages (does not)    hit rate   0.0%
```

Zero. Every page a scan loads is evicted by the pages behind it, so the pool does
no good *and* throws away what was in it. PostgreSQL confines large scans to a
ring buffer for exactly this.

### The cost model, recalibrated

`RANDOM_PAGE_COST` is gone: with a pool the axis that decides cost is hit against
miss, not sequential against random. `PAGE_HIT_COST = 0.36`,
`PAGE_MISS_COST = 1.0`, both measured, plus `distinct_pages_touched` (the
Cárdenas occupancy formula) so the planner can estimate how many fetches will
hit. The fit:

| Plan | Estimated | Measured | µs per unit |
|---|---:|---:|---:|
| index scan, 20 rows | 30 | 0.2 ms | 8.2 |
| index scan, 1 000 rows | 955 | 5.9 ms | 6.1 |
| index scan, 4 000 rows | 3 374 | 22.8 ms | 6.8 |
| index scan, 14 000 rows | 11 432 | 81.3 ms | 7.1 |
| sequential scan + filter | 11 703 | 77.3 ms | 6.6 |

And the crossover moved, correctly: `bucket < 200` (20% selectivity) was a
sequential scan in Milestone 6 and is an index scan now.

---

## Cost of correctness

| Feature | Cost | Why it is kept |
|---|---|---|
| CRC32 per page | one pass over the page on read and write | a torn write becomes a loud error instead of wrong answers |
| `validate()` on read | O(slots) | catches a corrupt header before it corrupts memory |
| Meta write per allocation | one extra page write | the free list and page count must survive a crash |
| Null bitmap always present | 1 byte per row per 8 columns | branch-free decoding |

The checksum is the expensive one — it touches the whole page. PostgreSQL makes
it optional (`data_checksums`) for exactly this reason. ChenDB keeps it on
because a teaching database that silently returns corrupt rows teaches the
wrong lesson; `Pager(verify_checksums=False)` exists for benchmarking and for
the inspector.

## Frontend

- **Events are batched.** The stream stages incoming events in a ref and
  flushes every 100 ms. One React state update per event would make the UI
  slower than the engine it is watching.
- **Buffers are bounded.** 2000 events client-side, oldest dropped, count
  reported.
- **The hexdump is capped** at 8 KiB rendered, with the remainder noted.
- **Queries are invalidated precisely.** An insert invalidates records, pages
  and the database summary — not the database *list*.

## What Milestone 7 should change

The buffer pool is the first milestone whose entire justification is
performance, so these are the numbers to beat:

- Repeated point lookups on the same page: *n* reads today, 1 read + *n*−1
  cache hits after.
- A scan run twice: 2*p* reads today, *p* + *p* hits after.
- Insert-heavy workloads: one write per row today; a dirty page written once
  per eviction after.

`PageReadEvent.source` already distinguishes `"disk"` from `"buffer_pool"`, so
the visualizer will be able to colour hits and misses on day one of that
milestone.
