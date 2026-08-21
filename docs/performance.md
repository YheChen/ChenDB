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

Measured on Python 3.14, Apple silicon, APFS on NVMe, 2000 rows, two full
scans, 100 point reads, 40 deletes:

| Level | Time | vs `OFF` | Events | Marginal cost |
|---|---|---|---|---|
| `OFF` | 0.0450 s | 1.00× | 0 | — |
| `SUMMARY` | 0.0450 s | 1.00× | 6 | below noise |
| `OPERATOR` | 0.0445 s | 0.99× | 6 | below noise |
| `STORAGE` | 0.0549 s | 1.22× | 6 395 | 1.55 µs/event |
| `VERBOSE` | 0.0546 s | 1.21× | 6 495 | 1.48 µs/event |

`OPERATOR` measuring marginally *faster* than `OFF` is noise, not a finding.
It emits the same six events as `SUMMARY`, and the difference is under 1%.
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
Beyond that the oldest events are dropped and counted, never silently.

## Indexes: Milestone 5

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
matching row. The *same* page repeatedly when matches share it, because there is
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
leaves in one pass (what a real system does) would be O(n) after the sort and
would leave no wasted space. The ordered scan is free ordering that nothing
exploits yet: there is no `ORDER BY`.

---

## The cost model: Milestone 6

The constants in `engine/optimizer/cost.py` are **measured for this engine**,
not copied from PostgreSQL, and the difference is the point. PostgreSQL's
defaults make CPU a hundred times cheaper than a page read; here it is about a
seventh, because a page read hits the OS cache while a row costs interpreted
Python.

```python
PAGE_COST          = 1.0    # pread + CRC32 over the page. The unit
RANDOM_PAGE_COST   = 1.0    # no buffer pool yet, so locality is nearly free
CPU_TUPLE_COST     = 0.15   # decode one record
CPU_PREDICATE_COST = 0.05   # evaluate a predicate on an already-decoded row
CPU_INDEX_COST     = 0.005  # compare one key inside a node, a memcmp
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
second part is what matters. A model that is internally consistent but
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
an exact distinct-value set per column. A real system samples (PostgreSQL reads
about 30,000 rows however large the table is) because a full scan to refresh an
*estimate* is absurd past a certain size.

Milestone 7's buffer pool makes `RANDOM_PAGE_COST` mean something and will move
the crossover. The µs-per-unit column above is the regression test: if it stops
being flat, the model needs recalibrating before the planner can be trusted.

---

## The buffer pool: Milestone 7

Measured on the read path, 4 KiB pages:

```
 pread, OS cached                303 ns
 CRC32 over the page             103 ns
 build a Page object             249 ns
 pool.fetch (a 4 KiB memcpy)     228 ns
 validate(): walks every slot 13 290 ns   ← 130x the checksum
```

`Page.validate()` was the entire cost of a page read, and it is bookkeeping:
one `struct.unpack_from` per slot, about a hundred on a full page. Splitting it (
O(1) header checks on the read path, the slot walk explicit) plus skipping
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

## Transactions: Milestone 8

The undo log sits on the path of **every page write in the engine**, so the
question is what it costs when it is doing nothing. Measured against a 41 ns
loop floor:

| Call | Median | What it does |
|---|---|---|
| `before_write`, no transaction open | 42 ns | one `is None` check |
| `before_write`, page already captured | 84 ns | one set lookup |

An insert costs about 13 µs, so the hook is under 1% either way. It is the same
rule the event system follows: **machinery that is always on has to be nearly
free when it is off.**

The current page image is passed as a **callable rather than bytes**, so the
page is only read when a snapshot is actually going to be kept. Thanks to
first-write-wins that is the minority of writes by a wide margin.

### The undo log is measured in pages, not rows

```
  20,000 rows inserted inside one transaction

    page writes seen      20,352
    before-images kept        91      <- 223x fewer
    undo log                 364 KiB
    rollback                 111 us
```

A page is captured the first time it changes and never again, so the log grows
with *distinct pages touched*. A logical undo log would have held 20,000
records; this one held 91 snapshots. That ratio is the entire reason whole-page
snapshots are affordable, see `docs/milestone-08-transactions.md`.

### Rollback

| Rows in the transaction | Pages held | Rollback |
|---|---|---|
| 100 | 4 | 10.3 µs |
| 1,000 | 8 | 11.0 µs |
| 5,000 | 25 | 27.1 µs |
| 20,000 | 91 | 111 µs |

Nearly all of the small numbers is fixed overhead, reloading the meta page and
invalidating the catalog and statistics caches. The marginal cost is about 1 µs
per page: one memory copy plus one write through the buffer pool.

Compare PostgreSQL, where a rollback is O(1) because nothing is undone at all,
aborted tuples stay in the heap, invisible, until `VACUUM`. That is MVCC paying
for itself, and it is the trade ChenDB makes in the other direction until
Milestone 10.

## The write-ahead log: Milestone 9

### What logging costs a write

| | Median |
|---|---|
| insert, no log | 14.4 µs |
| insert, logged | 18.7 µs (+30%) |

Every page write now also encodes a record, checksums it, and stamps an LSN into
the page, and that last one means a second CRC32 pass over the whole page,
because the LSN lives inside the range the page checksum covers.

### What a commit costs

| | Median | Ceiling |
|---|---|---|
| commit, fsync on | 88.7 µs | 11,300 commits/s |
| commit, fsync off | 29.2 µs | 34,300 commits/s |
| **the fsync** | **59.5 µs** | |

That ceiling has **nothing to do with how much work each transaction did**. It is
the disk, once per commit. Real systems amortise it with **group commit** (
several concurrent committers sharing one flush) which is meaningless with one
writer and is the obvious first optimisation once Milestone 10 brings a second.

`set_sync_policy(sync_on_commit=False)` exists so this table can be measured, not
as a durability option. It is the same line SQLite draws at
`synchronous=NORMAL` versus `FULL`: NORMAL survives a process crash, not a
machine crash.

### Log volume, and the coalescing that made it shippable

The first working version logged a whole page image per write:

| 20,000 rows in one transaction | Naive | With coalescing |
|---|---|---|
| log size | 81.1 MiB | **2.10 MiB** |
| amplification over the data | 197× | **5.1×** |
| records written | 22,516 | 516 |
| insert cost | 25.8 µs | 18.7 µs |

Writing the same page twice in a row produces two records of which only the
second matters, redo replays them in order and the first is immediately
overwritten. So an append whose predecessor is a **still-staged** update to the
same page by the same transaction replaces it rather than following it. 98% of
records in a bulk insert go that way.

Safe only while staged: once flushed, a page carrying that LSN may already be on
the disk, and the write-ahead rule guarantees the two windows cannot overlap.

What it does not fix is amplification across flush boundaries. Fixing that means
logging **deltas** rather than pages, which is what real systems do, see
`docs/milestone-09-wal.md`.

### Recovery

| Log | Records | Time |
|---|---|---|
| 113 KiB | 22 | 0.6 ms |
| 1.1 MiB | 222 | 2.2 ms |

Linear in log size, which is what checkpoint frequency is for. A checkpoint on a
2.5 MiB log takes 0.2 ms and takes it to zero.

## MVCC: Milestone 10

### Per row, on disk

Eight bytes of tuple header. On a 34-byte row that is 24%, paid by every row
whether or not anything ever reads concurrently. PostgreSQL's is 23 bytes.

### Per row, on read

| | |
|---|---|
| `read_tuple_header` | 250 ns |
| `visible()` | 83 ns |

The header is read **before** the row is decoded. That ordering is the only
reason the read cost is bearable: an invisible version costs 250 ns of
unpacking rather than a walk of every column.

### What dead versions do to a scan

| | Per row returned |
|---|---|
| 5,000 live rows | 1,427 ns |
| 2,500 live + 2,500 dead | 2,043 ns (**+43%**) |
| after vacuum | 1,496 ns |

A reader pays for every dead version it walks past. That is the price of never
blocking a writer, and `OperatorStats.rows_skipped` counts it. A number that
grows and never falls is an overdue vacuum, seen from the plan view.

### Locking

| | |
|---|---|
| uncontended acquire | 958 ns |
| re-taking one already held | 333 ns |

The second is on the hot path: a transaction updating the same row twice must
not wait for itself.

## UPDATE and DELETE: Milestone 11

2,000 rows, one statement, 4 KiB pages. `pay` is the only column changed and
only one of the two indexes covers it.

| indexes | `UPDATE` | `DELETE` | `INSERT` for scale |
|---|---|---|---|
| none | 52 µs/row | 17 µs/row | 20 µs/row |
| one | 123 µs/row | 55 µs/row | |
| two | 186 µs/row | 85 µs/row | |

Two things fall out of this table.

**Each index costs an update about 65 µs and a delete about 35 µs**: and the
update pays roughly double because it does a B+ tree *delete and insert* per
index where the delete does only the delete. That is the true price of the fact
that an MVCC update moves the row: the entry has to be taken out and put back
even when the key did not change.

**An update with no indexes still costs 2.5× a delete** (52 against 17). The
delete writes eight bytes of header; the update writes those eight bytes, then
encodes a whole new row and appends it. An update is genuinely two writes.

For comparison, an index-free update at 52 µs against a bare insert at 20 µs is
about the ratio you would predict from "delete plus insert plus one extra page
read to fetch the version being replaced".

### One quadratic, found by this milestone

`WriteAheadLog.next_lsn` is read once per append and used to compute
`sum(len(chunk) for chunk in self._buffer)`: O(n) per append, therefore O(n²)
across a transaction.

| | 2,000-row `UPDATE`, two indexes |
|---|---|
| before | 9.75 s, **8.5 s of it inside `sum`** (65 million `len` calls) |
| after | 1.16 s, no single function above 7% |

Keeping a running byte total is four lines. The bug had been in place since
Milestone 9 and never fired, because nothing before this staged thousands of
records inside one transaction, `insert_many` coalesces consecutive writes to
the same page, and an update alternates between two pages so it cannot.

`test_the_staged_total_is_tracked_rather_than_recomputed` pins the invariant.
The lesson is the ordinary one: the module docstring described an append as O(1)
and had said so, confidently, for two milestones.

## Cost of correctness

| Feature | Cost | Why it is kept |
|---|---|---|
| CRC32 per page | one pass over the page on read and write | a torn write becomes a loud error instead of wrong answers |
| `validate()` on read | O(slots) | catches a corrupt header before it corrupts memory |
| Meta write per allocation | one extra page write | the free list and page count must survive a crash |
| Undo capture, first write to a page | one page copy | a rollback that restores bytes needs the bytes |
| LSN stamp per page write | a second CRC32 over the page | the LSN is inside the checksum's range, and without it recovery cannot tell an applied change from an unapplied one |
| One `fsync` per commit | ~60 µs | the only thing that distinguishes a finished transaction from an interrupted one after a power cut |
| 8-byte tuple header per row | 24% of a small row | a reader that never waits for a writer needs to know which version it is looking at |
| Materialising an UPDATE/DELETE's matched rows | memory proportional to rows matched | without it the scan reaches versions the statement just wrote, the Halloween problem |
| Rewriting every index on an update | ~65 µs per index per row | the row's address changed, and no index knows that until it is told |
| Visibility check per row scanned | ~330 ns | a dead version has to be walked past, and walking past it is cheaper than blocking |
| Null bitmap always present | 1 byte per row per 8 columns | branch-free decoding |

The checksum is the expensive one, it touches the whole page. PostgreSQL makes
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
  and the database summary, not the database *list*.

## Where the remaining time goes

Eleven milestones in, the shape of a query's cost is:

```
  SELECT with a filter, 5,000 rows

    ~1,400 ns per row   decode + predicate, in interpreted Python
      ~330 ns per row   tuple header + visibility          (M10)
       ~50 ns per row   amortised page read from the pool  (M7)
```

**The interpreter is the wall.** Milestone 6 measured the cost model at ~18.9 µs
per cost unit and found it flat across a 650× range; Milestone 7 found the real
cost of a page read was `validate()` rather than the syscall. Neither of those
findings changes here, and the per-row constant has grown by about a quarter
because every row now carries a version.

Making this materially faster is not a matter of a better algorithm. It is
vectorised execution, or a compiled inner loop, or not being in Python, and
each of those trades the thing this project is for, which is that every line of
it can be read.

---

## The rest of the engine, measured: five more benchmarks

Milestones 7 to 10 reported numbers in prose that no committed script could
reproduce. These five run on demand and print their own tables, so a regression
shows up as a moved number rather than as a paragraph nobody re-checks.

```bash
make bench-all                          # all seven, in order
python benchmarks/buffer_pool.py        # hit rate, flooding, write-back
python benchmarks/btree_inserts.py      # build cost, index maintenance, height
python benchmarks/joins.py              # hash against nested loop, build side
python benchmarks/transactions.py       # commit throughput, fsync, rollback
python benchmarks/recovery.py           # ARIES passes, checkpoint cost
```

Everything below is from one run on Python 3.14, Apple silicon, APFS on NVMe.
Absolute times are a property of that machine. The ratios are not.

### The buffer pool: a hit rate is about the workload

600 rows in 86 pages of 256 bytes, so seven rows to a page.

| Workload | 4 frames | 8 | 16 | 32 | 128 |
|---|---:|---:|---:|---:|---:|
| working set of ~6 pages | 84.5% | 97.5% | 97.5% | 97.5% | 97.5% |
| full scan of all 86 pages | 0.0% | 0.0% | 0.0% | 0.0% | 74.6% |

**Zero, four times over.** A scan larger than the pool evicts every page it
loads before the next pass wants it, so the cache does no good *and* throws away
what was in it. That is sequential flooding, and it is why PostgreSQL confines a
large scan to a ring buffer. The working-set row is the opposite case and needs
almost nothing: eight frames is already enough.

What residency is worth, over 400 point reads of the same 40 rows:

| Pool | µs per read | hit rate | preads |
|---|---:|---:|---:|
| holds the working set | 2.09 | 99.4% | 8 |
| two frames | 2.24 | 84.9% | 188 |

The syscall count collapses by a factor of twenty. The **time barely moves**,
and that is the honest reading: a `pread` of a page the OS has cached is cheap
next to an interpreted `decode_record`. It is the same finding the cost model was
calibrated on (`CPU_TUPLE_COST` is about a seventh of a page, not a hundredth),
and it means a warm full scan through a large pool is not measurably faster here
than through a small one.

Write-back is where this pool earns its memory outright:

```
600-row insert:  2,048 logical writes -> 75 syscalls   (96% absorbed)
                 1,845 of them replaced a page that was already dirty
```

### B+ trees: what a build costs and what an index costs to keep

| Rows | Build | µs/row | Height | Splits | Pages | Bytes per entry |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 28.7 ms | 28.7 | 2 | 8 | 10 | 41 |
| 5,000 | 169.4 ms | 33.9 | 2 | 36 | 38 | 31 |
| 20,000 | 772.7 ms | 38.6 | 2 | 146 | 148 | 30 |

An entry is a key plus a record id, under twenty bytes. Thirty is the price of
splitting a full leaf in half and leaving both halves half empty, which is what
row-by-row insertion does and what bulk loading (sort first, pack leaves once)
would remove.

Insert cost with more indexes to maintain, 1,000 rows added to a 500-row table:

| Indexes maintained | Time |
|---|---:|
| primary key only | 139.1 ms |
| plus one secondary | 165.5 ms |
| plus two | 230.9 ms |

The first row is not "no index": a `PRIMARY KEY` here builds a real unique B+
tree, so it is already one structure being kept true on every insert. This is
the cost that makes an *unused* index worse than no index.

### Joins: how fast the gap opens

| customers x orders | Rows out | Hash join | Nested loop | Ratio |
|---|---:|---:|---:|---:|
| 100 x 400 | 400 | 2.4 ms | 32.9 ms | 13.8x |
| 400 x 1,600 | 1,600 | 9.3 ms | 498.9 ms | 53.7x |
| 1,600 x 6,400 | 6,400 | 37.3 ms | not run | 10.2M pairs |

`PlannerOptions` has switches for access paths and none for join algorithms, so
the loop is forced by planning the query and swapping the hash node for a
nested-loop node over the same inputs: same plan, same answer, one different
algorithm. The largest case is skipped and *says* it is skipped, because a
benchmark that quietly drops a row reads like one that ran it.

The build side is chosen correctly at every size (the smaller estimate, always
`customers`), and a foreign-key join estimates exactly: 400, 1,600 and 6,400
predicted against 400, 1,600 and 6,400 returned.

### Transactions: the fsync is the whole story, until it is not

1,000 rows, spread over more or fewer transactions.

| Rows per txn | Commits | fsyncs | fsync on | fsync off | Difference |
|---:|---:|---:|---:|---:|---:|
| 1 | 1,000 | 1,004 | 139.6 ms | 88.7 ms | 50.9 ms |
| 10 | 100 | 104 | 81.5 ms | 71.9 ms | 9.7 ms |
| 100 | 10 | 14 | 69.4 ms | 68.5 ms | 0.9 ms |
| 1,000 | 1 | 5 | 71.6 ms | 71.0 ms | 0.5 ms |

About 50 µs per `fsync` on this disk, and a thousand of them is a third of the
row-at-a-time run. Batching does not make the writes cheaper; it makes the
*durability points* fewer, which is the only part that was expensive. By 100 rows
per transaction the difference is inside run-to-run noise, which is worth
printing rather than rounding into a claim.

Rollback, which is the operation this design pays for and PostgreSQL does not:

| Rows inserted | Insert | Rollback | Pages undone |
|---:|---:|---:|---:|
| 10 | 0.4 ms | 0.0 ms | 2 |
| 100 | 4.5 ms | 0.0 ms | 2 |
| 1,000 | 72.2 ms | 0.2 ms | 22 |

Undo is *physical*, so the cost tracks pages rather than rows: a hundred rows
land in the same two pages and cost the same to take back as ten.

### Recovery: three passes, and what a checkpoint bounds

A crash here is `Database.abandon()` with a four-frame pool, so eviction forces
log records to disk the way a large transaction on a real database would.
`tests/recovery/` is the stronger version: it kills a child process with
`SIGKILL` so no Python cleanup can run at all.

An **interrupted** transaction, which recovery must take back:

| Rows lost | Records | Redone | Skipped | Undone | Recover | Rows after |
|---:|---:|---:|---:|---:|---:|---:|
| 100 | 0 | 0 | 0 | 0 | 0.1 ms | 0 |
| 1,000 | 2,006 | 43 | 1,963 | 21 | 7.8 ms | 0 |
| 5,000 | 10,332 | 3 | 10,329 | 94 | 42.8 ms | 0 |

A **committed** one the crash caught before its pages reached the file:

| Rows | Records | Redone | Skipped | Undone | Recover | Rows after |
|---:|---:|---:|---:|---:|---:|---:|
| 100 | 201 | 200 | 0 | 0 | 1.5 ms | 100 |
| 1,000 | 2,068 | 104 | 1,963 | 0 | 7.7 ms | 1,000 |
| 5,000 | 10,356 | 26 | 10,329 | 0 | 38.5 ms | 5,000 |

Every row survives, without a single page having been forced at commit time.
That is no-force, and redo is what pays for it. The smallest interrupted case
recovers *nothing* because nothing was ever evicted, so nothing was durable: a
correct outcome that a benchmark should show rather than hide.

Where the time goes, on the 5,000-row case:

| Pass | Time | Share |
|---|---:|---:|
| analysis | 2.1 ms | 15% |
| redo | 11.0 ms | 77% |
| undo | 1.1 ms | 8% |

And the checkpoint that bounds all of it, after 5,000 inserted rows:

```
log before      41.3 MiB      ← a full before-image and after-image per page
log after        0.0 KiB
pages flushed        94
checkpoint        0.9 ms
```

41 MiB of log for a few hundred KiB of rows is the write amplification this
design pays, and a sharp checkpoint is what reclaims it. It is also what lets
analysis skip a dirty-page table: recovery never needs to start earlier than the
last checkpoint.
