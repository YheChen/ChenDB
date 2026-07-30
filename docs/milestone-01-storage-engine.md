# Milestone 1: Page-based storage and the storage inspector

**Status: complete.** Engine version 0.1.0.

## Goal

Make a database file that is real: fixed-size pages, a slotted page layout,
binary records, a heap, and a metadata page, then prove it by inserting rows,
restarting the process, and inspecting the persisted bytes in the visualizer.

---

## What was built

### Engine (`engine/`, standard library only)

| Module | Responsibility |
|---|---|
| `storage/constants.py` | page sizes, page types, sentinels, the format's vocabulary |
| `storage/page.py` | slotted page: insert, read, delete, compact, checksum, validate |
| `storage/meta.py` | page 0, magic, version, page count, free list, root pointers |
| `storage/pager.py` | page id → file offset; allocation; free list; `fsync`; I/O stats |
| `storage/heap.py` | `RecordId`, a chain of pages holding one table |
| `storage/inspect.py` | read-only views: page summaries, header fields, slots, hexdump |
| `serialization/types.py` | codecs for INTEGER, FLOAT, BOOLEAN, TEXT |
| `serialization/schema.py` | `Column`, `Schema`, `TableDescriptor` |
| `serialization/record.py` | row ⇄ bytes with a null bitmap; per-field layout |
| `diagnostics/` | `TraceLevel`, 11 event types, four sinks, the tracer |
| `database.py` | the `Database` facade |
| `__main__.py` | `python -m engine`, an interactive storage explorer |

### Server (`engine/server/`)

FastAPI + Pydantic, the only place web dependencies appear. Workspace with path
containment, a per-database lock, the engine→API mapper boundary, routers for
databases / pages / records / events, and a WebSocket event stream with
bounded, reported backpressure.

### Visualizer (`visualizer/`)

React + TypeScript + Vite + Tailwind + TanStack Query. Database picker, schema
browser and structural table builder, results table with real cost figures,
disk map, page inspector (layout / header / slots / hex), and a live event
timeline. Dark and light themes, resizable panels, keyboard-operable dividers.

Monaco, React Flow and D3 are **not** dependencies yet. There is no SQL to edit
and no plan to draw; they arrive with Milestones 2, 3 and 5.

---

## The demo

```bash
# 1. Create a database with deliberately small pages and insert some rows.
python -m engine demo.chendb \
  -c ".create users id:INTEGER* email:TEXT! age:INTEGER" \
  -c ".insert 1 | ada@example.com | 36" \
  -c ".insert 2 | alan@example.com | NULL"

# 2. The process is gone. Start a new one: the rows are still there.
python -m engine demo.chendb -c ".scan" -c ".pages"
```

```
rid        id                email             age
---------------------------------------------------------------
(3,0)      1                 ada@example.com   36
(3,1)      2                 alan@example.com  NULL
(2 row(s))
  id  type       offset slots  live   free   dead  next  ck  owner
   0  META            0     0     0   4036      0     -  ok  meta
   1  SCHEMA       4096     1     1   3827      0     -  ok  schema
   2  HEAP         8192     2     2   4007      0     -  ok  users
```

Then look at the bytes:

```bash
python -m engine demo.chendb -c ".page 2"
```

```
slot directory
  slot 1   offset=4039  length=24    0402000000000000000b000000416c616e20547572696e67
        id               INTEGER  @1+8         2
        name             TEXT     @9+15        Alan Turing
        age              INTEGER  NULL         NULL
```

`04` is the null bitmap, bit 2 set, so `age` is NULL and occupies no bytes.
`02 00 …` is the int64. `0b 00 00 00` is the text length, 11, followed by
`Alan Turing`. Nothing is hidden.

And in the browser:

```bash
python -m engine.server --workspace workspace   # terminal 1
npm --prefix visualizer run dev                 # terminal 2
```

---

## Why real databases do this

**Fixed-size pages.** Page *n* is at `n × page_size`. That one identity makes
the page id a random-access address with no index, keeps free-space management
tractable, and lets a buffer pool cache uniform frames. Every disk-based
database does this; the only real question is the size. SQLite defaults to
4096, PostgreSQL to 8192.

**Slotted pages.** Variable-length rows in a fixed-size block need an
indirection layer. The slot directory means a row can be *moved within its
page* without invalidating any external reference, which is what makes
compaction possible and what lets an index store physical addresses. This is
PostgreSQL's `PageHeaderData` + `ItemIdData` almost exactly.

**A null bitmap.** Storing "absent" in one bit rather than a sentinel value
keeps NULL out of the value domain, `NULL` and `0` are genuinely different,
and three-valued logic depends on it. PostgreSQL and SQLite both do this;
PostgreSQL additionally omits the bitmap when a tuple has no NULLs.

**Checksums.** A torn write is when the OS persisted part of a page before
losing power. Without a checksum you get silently wrong query results. With
one, you get a loud error. PostgreSQL ships `data_checksums`, ZFS checksums
every block, and InnoDB has had page checksums since the beginning.

**A metadata page at offset 0.** Every persistent structure has to be reachable
from a known location. SQLite puts a 100-byte header at the start of page 1;
PostgreSQL uses a separate `pg_control` file so the header survives damage to
the heap.

---

## Complexity

| Operation | Cost | Notes |
|---|---|---|
| `page.insert` | O(1), O(slots) when it compacts | in memory |
| `page.read(slot)` | O(1) | one array index |
| `page.delete(slot)` | O(1) | write a tombstone |
| `page.compact` | O(slots + live bytes) | slot ids preserved |
| `pager.read_page` | O(1): 1 seek + 1 read | a syscall until Milestone 7 |
| `pager.allocate_page` | O(1) + 1 meta write | free-list pop or file extension |
| `heap.insert` | O(1): 1 read, 1–2 writes | tail page only |
| `heap.get(rid)` | O(1): 1 read | |
| `heap.scan` | O(pages) reads, O(rows) work | lazy generator |
| `heap.count` | O(pages) | no cached count |
| `encode`/`decode` | O(row bytes) | O(k) to reach column *k* |

A full scan of *n* rows at *r* rows per page is ⌈n/r⌉ reads. On 256-byte pages
with ~30-byte rows that is 7 rows per page: 300 rows → 43 page reads, all of
them syscalls today.

---

## Measured

`python benchmarks/trace_overhead.py`: 2000 rows, two scans, 100 point reads,
40 deletes. Python 3.14, Apple silicon, APFS on NVMe.

| Trace level | Time | vs `OFF` | Events |
|---|---|---|---|
| `OFF` | 0.0450 s | 1.00× | 0 |
| `SUMMARY` | 0.0450 s | 1.00× | 6 |
| `STORAGE` | 0.0549 s | 1.22× | 6 395 |
| `VERBOSE` | 0.0546 s | 1.21× | 6 495 |

Full-detail tracing costs about 22%, and every level produced a
**byte-identical database file**.

The guarded-emit pattern is what keeps `OFF` free. With tracing off, over
200 000 call sites:

| Pattern | Per call |
|---|---|
| `if tracer.storage: tracer.emit(...)` | 8.5 ns |
| `tracer.emit(...)` | 296.0 ns |

**35×.** Python evaluates arguments before the call, so the unguarded form
builds the event and discards it.

Storage primitives with tracing off, 4096-byte pages, ~87 rows per page:

| Operation | Cost |
|---|---|
| insert | 18.8 µs/row |
| full scan | 1.6 µs/row (23 page reads for 2000 rows) |
| point read by `RecordId` | 17.0 µs/row |
| `fsync` | 0.9 µs (median) |

A point read costs as much as an insert because both are one page read, and
there is no cache. Milestone 7 is the first change that should move that number.

---

## Tests

282 Python tests, 42 frontend tests.

| Area | File | Covers |
|---|---|---|
| Slotted page | `tests/unit/test_page.py` | layout, fill, tombstones, compaction, checksums, corruption, churn |
| Records | `tests/unit/test_record.py` | round-trips, null bitmap, type errors, truncation, field offsets |
| Pager | `tests/unit/test_pager.py` | create/reopen, allocation, free list, torn pages, truncation |
| Heap | `tests/unit/test_heap.py` | chaining, O(1) append, lazy scan, delete, reuse, 500-row stress |
| Diagnostics | `tests/unit/test_diagnostics.py` | levels, retention, drops, snapshots, thread safety |
| Boundaries | `tests/unit/test_architecture_boundaries.py` | the four architecture rules |
| Backpressure | `tests/unit/test_ws_backpressure.py` | drop-oldest policy, never blocks |
| Inspection | `tests/unit/test_inspect.py` | summaries, field offsets, damaged pages |
| Persistence | `tests/integration/test_persistence.py` | restart, multi-restart, every page size |
| Tracing | `tests/integration/test_tracing.py` | byte-identical files at every level |
| API | `tests/integration/test_api.py` | all endpoints, traversal, snapshot consistency |
| WebSocket | `tests/integration/test_websocket.py` | lifecycle, subscription release, no blocking |
| Crash | `tests/recovery/test_crash_and_corruption.py` | `SIGKILL`, torn pages, truncation |

Two are worth calling out.

**Crash tests use `SIGKILL` on a child process.** No `close()`, no `fsync`, no
atexit hooks. Any cooperative shutdown would quietly flush the buffers whose
loss is under test.

**Tracing tests compare file bytes, not just rows.** Running the same workload
at `OFF`, `STORAGE` and `VERBOSE` must produce byte-identical files.
Observation does not change the system observed.

---

## Acceptance criteria

- [x] A database file is a whole number of fixed-size pages, identifiable by
      its magic string.
- [x] Rows inserted before a process exits are readable after a new one starts.
- [x] The schema survives a restart, so rows can still be decoded.
- [x] `RecordId`s remain valid across a restart.
- [x] Every page carries a checksum; a single flipped bit is detected.
- [x] Deletes are tombstones; space is reclaimed by compaction; slot ids survive it.
- [x] Freed pages are recycled instead of growing the file.
- [x] Page sizes 256–8192 all round-trip.
- [x] A `SIGKILL`ed process leaves an openable file with all synced rows intact.
- [x] Truncation, bad magic and torn pages are detected, not silently tolerated.
- [x] Diagnostics are optional, bounded, and provably result-neutral.
- [x] `import engine` works with zero third-party packages installed.
- [x] The API exposes no filesystem paths and rejects traversal.
- [x] A slow or vanished WebSocket client cannot block a query.
- [x] The visualizer shows real bytes from the real file.
- [x] Unimplemented features are absent from the API and hidden in the UI.

---

## Known limitations

Each is deliberate and has a milestone attached.

| Limitation | Resolved by |
|---|---|
| One table per database file | M4 (the catalog |
| Tables defined structurally, not with `CREATE TABLE` | M2) the parser |
| Schema stored as JSON on a chain of pages | M4, real system tables |
| Insert only tries the tail page; earlier free space is stranded | M7, a free space map |
| Records larger than one page fail | overflow pages (unscheduled) |
| Every page read is a syscall | M7, the buffer pool |
| `count()` is a full scan | unscheduled; PostgreSQL has the same behaviour |
| No transactions, no isolation, no rollback | M8 |
| A crash between write and `fsync` loses recent rows | M9 (the WAL |
| A torn page is detected but cannot be repaired | M9) redo |
| One writer; no concurrency control | M10 |
| O(k) to reach column *k* of a row | unscheduled; needs cached offsets |

---

## Next: Milestone 2, SQL parser and AST explorer

**Engine.** Tokenizer, recursive-descent parser, AST, and `CREATE TABLE`,
`INSERT`, `SELECT` with basic `WHERE`. Events: `TokenEvent`,
`AstNodeCreatedEvent`, `ParseErrorEvent`.

**Visualizer.** Monaco SQL editor, token stream, AST tree, source-span
highlighting, errors at the right position.

**Demo.** Select any AST node and watch the SQL text that produced it light up.

Milestone 2 also retires the `.create` dot-command and the structural table
builder, replacing both with real SQL.
