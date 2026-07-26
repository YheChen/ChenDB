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
