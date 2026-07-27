# Architecture

## The four layers

```
┌─────────────────────────────────────────────────────────────────────┐
│  visualizer/            React · TypeScript · Vite · Tailwind         │
│  A pure client. Holds no engine state of its own.                    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  HTTP + WebSocket, JSON
┌───────────────────────────────▼─────────────────────────────────────┐
│  engine/server/         FastAPI · Pydantic · uvicorn                 │
│  Workspace + path containment · engine lock · mappers · routers      │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  Python function calls
┌───────────────────────────────▼─────────────────────────────────────┐
│  engine/                Standard library only                        │
│  Database · Heap · Pager · Page · serialization · diagnostics        │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  read / write / fsync
┌───────────────────────────────▼─────────────────────────────────────┐
│  one file of fixed-size pages                                        │
└─────────────────────────────────────────────────────────────────────┘
```

Dependencies point in exactly one direction. Four rules make that concrete, and
`tests/unit/test_architecture_boundaries.py` fails the build if any is broken:

1. Nothing under `engine/` except `engine/server/` may import `fastapi`,
   `starlette`, `uvicorn`, `pydantic` or `anyio`.
2. Nothing under `engine/` except `engine/server/` may import *anything*
   outside the standard library.
3. `engine/server/` imports `engine`; never the reverse.
4. Diagnostic events define no `to_json`, `model_dump` or equivalent —
   serialization lives only in `engine/server/mappers.py`.

The payoff is concrete: `import engine` works in an interpreter with zero
installed packages, and the storage engine's own test suite runs without a web
framework anywhere in sight.

## Inside the engine

```
engine/
├── database.py            Database — the public facade
├── errors.py              one exception tree for the whole engine
│
├── storage/
│   ├── constants.py       page size, page types, sentinels
│   ├── page.py            slotted page: records in a fixed-size block
│   ├── meta.py            page 0 — root of every persistent structure
│   ├── pager.py           page id → file offset; allocation; checksums; fsync
│   ├── buffer.py          the page cache: frames, write-back, LRU
│   ├── heap.py            a chain of pages holding one table
│   └── inspect.py         read-only views of all of the above
│
├── serialization/
│   ├── types.py           per-type codecs (INTEGER, FLOAT, BOOLEAN, TEXT)
│   ├── schema.py          Column, Schema, TableDescriptor
│   └── record.py          row ⇄ bytes, with a null bitmap
│
├── diagnostics/
│   ├── levels.py          TraceLevel, EventCategory
│   ├── events.py          frozen dataclasses, one per observable operation
│   ├── sink.py            Null / RingBuffer / Callback / Fanout
│   └── tracer.py          the emit path, with fast-path flags
│
├── index/
│   ├── key.py             order-preserving key encoding — memcmp means <
│   ├── node.py            one slotted page read as a B+ tree node
│   └── bplustree.py       search, insert, split, range scan, delete
│
├── catalog/
│   ├── system.py          the system tables' own schemas, compiled in
│   └── catalog.py         Catalog — tables and indexes, with a cache
│
├── transaction/
│   ├── undo.py            UndoLog — page id → before-image, first write wins
│   └── manager.py         begin / commit / rollback, and the write hook
│
└── parser/ planner/ optimizer/ executor/
    concurrency/ wal/                           ← Milestones 2, 6, 9, 10
```

Each layer knows only the one beneath it:

```
Database          rows of Python values
    │
    ├── HeapFile          records as bytes, addressed by RecordId
    │       │
    │       └── Pager     pages as bytes, addressed by page id
    │               │
    │               └── the file
    │
    └── serialization     Schema + codecs: values ⇄ bytes
```

That layering is what makes each milestone an *insertion* rather than a
rewrite. Milestone 7's buffer pool slides between `HeapFile` and `Pager`
without either of them changing, because the heap only ever asks for "the page
with this id" and the pager only ever answers with bytes.

Milestone 8's transactions are the same trick from the other direction. Every
write in the engine funnels through one method on the pager, so a single hook
there captures a before-image of any page about to change — and because the
undo log works in **pages rather than rows**, it never learns what a heap
record, a B+ tree node or a catalog row is. `CREATE TABLE` became atomic with
no change to `engine/catalog/`, and every index operation became undoable with
no change to `engine/index/`.

```
     HeapFile / BPlusTree / Catalog          none of them know
              │
              ▼
       Pager._write_at ───────▶ TransactionManager.before_write
              │                        │
              ▼                        ▼
        BufferPool                  UndoLog     one snapshot per page
```

## An insert, end to end

```
db.insert((1, "ada@example.com", 36, True))
    │
    │  engine/database.py
    ├─▶ encode_record(schema, values)
    │       null bitmap (1 byte) + int64 + len-prefixed UTF-8 + int64 + bool
    │       → b'\x00\x01\x00...'                                   29 bytes
    │
    │  engine/storage/heap.py
    ├─▶ HeapFile.insert(payload)
    │       │
    │       ├─▶ pager.read_page(last_page_id)          ← 1 read syscall
    │       │
    │       ├─▶ page.insert(payload)
    │       │       needs 29 + 4 bytes; free space is 67  → fits
    │       │       write payload at free_end − 29
    │       │       write slot entry (offset, length)
    │       │       free_start += 4 · free_end −= 29
    │       │
    │       └─▶ pager.write_page(page)                 ← 1 write syscall
    │               page.update_checksum()             ← CRC32 over the page
    │
    └─▶ RecordId(page_id=3, slot_id=4)

When the page is full instead:

    page.insert → None
        │
        ├─ page.would_fit_after_compaction()?  yes → compact, retry
        └─                                     no  → allocate + link a new page
                                                     pager.allocate_page(HEAP)
                                                     tail.next_page_id = new
                                                     meta.heap_last_page = new
```

Every step above emits a diagnostic event when the trace level allows it, which
is what the visualizer's timeline is showing.

## Diagnostics

```
   storage thread                                   consumers
   ──────────────                                   ─────────
   pager.read_page()
        │
        │  if tracer.storage:            ← one attribute load, then a branch
        ├──── tracer.emit(PageReadEvent(...))
        │           │
        │           ▼
        │      Tracer stamps seq + timestamp → TraceRecord
        │           │
        │           ▼
        │      FanoutSink ──┬──▶ RingBufferSink   bounded history, HTTP reads it
        │                   └──▶ CallbackSink ──▶ one per WebSocket connection
        ▼
   returns the page
```

The guard matters. Python evaluates arguments before the call, so an unguarded
`tracer.emit(PageReadEvent(...))` builds the event even at `TraceLevel.OFF`.
With the guard, tracing off costs a boolean test.

`tests/integration/test_tracing.py` asserts something stronger than "tracing is
cheap": the database files produced at `OFF`, `STORAGE` and `VERBOSE` are
**byte-identical**. Observation does not change the system observed.

## Concurrency and snapshot consistency

`Database` is not thread-safe — one file handle, one seek position. FastAPI
runs synchronous endpoints in a worker threadpool, so concurrent requests are
real. Each open database is therefore wrapped in a `ManagedDatabase` holding a
lock.

The rule for every diagnostics endpoint:

> Take the lock, copy an immutable snapshot, release the lock, *then* serialize.

```python
with managed.use() as db:          # lock held
    detail = db.page_detail(7)     # frozen dataclasses, copied out
                                   # lock released here
return mappers.page_detail_to_api(detail)   # pure CPU, no lock
```

Holding an engine lock across JSON encoding — let alone across a socket write —
would let one slow client stall every query. Copying instead means a response
can never show a page list assembled from two different instants.

## WebSocket backpressure

Events are produced on storage threads and consumed on the event loop. During a
large scan at `VERBOSE` the producer is faster than any browser. The policy:

- each connection owns a bounded queue (`CHENDB_WS_QUEUE_SIZE`, default 512);
- when it is full the **oldest** event is dropped and a counter increments;
- the count is sent to the client as a `dropped` frame and shown in the UI.

`Subscription.offer()` runs on the storage thread and only ever calls
`loop.call_soon_threadsafe`. It cannot block, so a browser tab that stops
reading can never apply backpressure to the storage engine. A diagnostics
channel that can stall a query is worse than no diagnostics channel.

## Where the visualizer sits

```
visualizer/src/
├── lib/api.ts          one typed fetch wrapper; the only place fetch appears
├── types/api.ts        GENERATED from the OpenAPI schema — do not hand-edit
├── hooks/
│   ├── useEngine.ts    TanStack Query hooks; structured keys for invalidation
│   ├── useEventStream.ts  WebSocket + bounded buffer + batched flush
│   └── useTheme.ts
├── components/         Panel, Button, Badge, SplitPane, ErrorBoundary
├── features/           one directory per panel
└── pages/ExplorerPage.tsx   the layout
```

`src/types/api.ts` is produced by `scripts/generate_api_types.py` from the
server's own OpenAPI schema. Renaming a Pydantic field breaks the TypeScript
build instead of failing at runtime in a browser.

## Adding a milestone

The shape of the work is the same each time:

1. **Engine.** Build the component under `engine/<area>/`, standard library
   only. Add its events to `engine/diagnostics/events.py`.
2. **Instrument.** Guard each emit with a cached tracer flag. Nothing in the
   engine learns what a consumer is.
3. **Map.** Add Pydantic models under `engine/server/schemas/` and conversions
   in `mappers.py`. Nowhere else.
4. **Expose.** Add a router. Flip the flag in `app.py:FEATURES`.
5. **Generate.** Re-run `scripts/generate_api_types.py`.
6. **Visualize.** Add a panel under `visualizer/src/features/`, shown only when
   its feature flag is true.
7. **Test.** Unit for the component, integration for the API, and a check that
   the new events do not alter query results.

See `docs/how-to-instrument.md` and `docs/how-to-add-a-panel.md` for the
step-by-step versions.
