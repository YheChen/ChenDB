# API contract — v1

Base path `/api/v1`. The live OpenAPI schema is served at
`/api/v1/openapi.json` and browsable at `/api/v1/docs`; a committed copy lives
at [`docs/openapi.json`](openapi.json).

Endpoints for features that do not exist yet are **absent**, not stubbed. Check
`features` on `/health` before rendering a panel.

## Resource naming

The original sketch used flat paths (`/api/v1/pages`, `/api/v1/query`). This
implementation nests under `/databases/{database_id}/` instead:

```
GET /api/v1/databases/demo/pages/3        not  GET /api/v1/pages/3
```

A workspace holds many databases, so a flat path needs an implicit "current
database" in server-side session state — invisible in the URL, awkward to
share, and racy when two browser tabs disagree. Nesting makes the resource
explicit. Versioning, the error envelope and the WebSocket path are unchanged.

## Milestone 1 surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | version, milestone, feature flags |
| `GET` | `/databases` | list databases in the workspace |
| `POST` | `/databases` | create one |
| `GET` | `/databases/{db}` | detail: pages, stats, schema, trace level |
| `DELETE` | `/databases/{db}` | close and delete the file |
| `POST` | `/databases/{db}/table` | define the table |
| `GET` | `/databases/{db}/table` | schema and storage stats |
| `POST` | `/databases/{db}/records` | insert rows |
| `GET` | `/databases/{db}/records` | scan, `?offset=&limit=` |
| `DELETE` | `/databases/{db}/records/{page}/{slot}` | tombstone one row |
| `GET` | `/databases/{db}/pages` | every page, summarised |
| `GET` | `/databases/{db}/pages/{page_id}` | full page inspection |
| `GET` | `/databases/{db}/events` | trace history, `?after_seq=&limit=&category=` |
| `DELETE` | `/databases/{db}/events` | clear retained events |
| `GET` `PUT` | `/databases/{db}/trace` | read / set the trace level |
| `WS` | `/databases/{db}/events/stream` | live events |

## Added since

Milestones 2–4 replaced the singular `/table` and `/records` with
`/tables/{table}` collections, and added `/catalog`, `/parse`, `/query`,
`/query/step` and `/executions/*`.

### Milestone 5 — indexes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/databases/{db}/indexes` | every index; `?table=` narrows to one |
| `POST` | `/databases/{db}/indexes` | create one over a column |
| `GET` | `/databases/{db}/indexes/{name}` | the tree, `?max_nodes=` (default 512) |
| `GET` | `/databases/{db}/indexes/{name}/search` | trace a point lookup, `?value=` |

The tree comes back as a **flat node list plus a root page id**, not nested
JSON — the same shape as the AST and the plan, for the same three reasons: a
client wanting one node need not walk a recursive structure; a cycle in a corrupt
tree becomes a visibly duplicated entry rather than a response that never ends;
and the renderer computes its own layout anyway.

```json
{
  "index": { "name": "users_age", "column_name": "age", "height": 3,
             "entry_count": 600, "page_count": 33, "unique": false, "...": "" },
  "tree": {
    "root_page_id": 82,
    "height": 3,
    "truncated": false,
    "nodes": [
      { "page_id": 82, "level": 2, "is_leaf": false,
        "keys": ["-∞", "37"], "children": [60, 81],
        "record_ids": [], "next_leaf_id": null,
        "free_bytes": 470, "entry_count": 2 },
      { "page_id": 67, "level": 0, "is_leaf": true,
        "keys": ["40", "40", "42"], "children": [],
        "record_ids": ["(6,0)", "(9,9)", "(13,6)"], "next_leaf_id": 74,
        "free_bytes": 13, "entry_count": 25 }
    ]
  },
  "stats": { "searches": 1, "splits": 30, "root_splits": 2, "...": 0 }
}
```

Keys arrive **already rendered as strings**. An encoded key is an
order-preserving byte string only `engine.index.key` can interpret; sending the
raw bytes would force the browser to reimplement the codec, and a visualizer
showing something different from the engine is the exact failure this project
exists to avoid. `"-∞"` is the sentinel separator every internal node starts
with.

`truncated` is `true` when `max_nodes` cut the response short. A client must
tolerate a `children` or `next_leaf_id` entry that names a page not in `nodes`.

`GET /indexes/{name}/search?value=42` runs one real lookup:

```json
{ "index_name": "users_age", "value": "42", "found": true, "matches": ["(6,0)"],
  "path": [82, 81, 67], "pages_visited": 4, "height": 3 }
```

`value` is text and is coerced to the index's declared type server-side — a query
string has no types, and guessing from JSON shape would make `?value=1`
ambiguous between the integer and the string. A value the index cannot encode is
`422 InvalidKey`, not a crash. `pages_visited` can exceed `path.length` when
duplicates span leaves and the search steps right.

### Milestone 6 — the planner

No new endpoints. `PlanModel` grows instead, because a plan belongs with the
query that produced it:

```json
{
  "root_id": "project_1",
  "nodes": [
    { "operator_id": "scan_1", "operator_type": "IndexScan",
      "detail": "index=users_age age = 30",
      "estimated_rows": 20, "estimated_cost": 26.2,
      "estimated_io_cost": 24.0, "estimated_cpu_cost": 2.2,
      "output_rows": 20, "duration_ns": 204000, "...": "" }
  ],
  "alternatives": [
    { "description": "Sequential scan of users", "access_path": "PhysicalSeqScan",
      "estimated_cost": 387.0, "estimated_rows": 2000, "chosen": false,
      "rejected_because": "14.8x the cost of the chosen plan", "index_name": null },
    { "description": "Index scan on users_age (age = 30)",
      "access_path": "PhysicalIndexScan", "estimated_cost": 26.2,
      "estimated_rows": 20, "chosen": true, "rejected_because": "",
      "index_name": "users_age" }
  ],
  "rewrites": ["fold_constants"],
  "estimated_cost": 26.3,
  "statistics": { "table_name": "users", "row_count": 2000, "page_count": 87,
                  "stale": false, "gathered_at_ns": 1730000000000000000 }
}
```

Every operator carries **estimated beside actual**. The gap is the single most
useful thing a plan view can show: a slow plan is almost always one whose row
estimate was wrong, and no amount of staring at the chosen operators reveals
that. `estimated_cost` on a node is cumulative — this node plus everything below
it — matching what `EXPLAIN` prints.

Every candidate is reported, not just the winner, each with why it lost.

`statistics.stale` is `true` when the table has been written to since it was last
analyzed. The estimates are still computed from the old numbers, and are still
used — a slightly stale estimate beats none. Saying so is the alternative to
pretending otherwise.

`EXPLAIN` and `ANALYZE` go through the ordinary `POST /query`. `EXPLAIN` returns
a one-column `QUERY PLAN` result set, so any client that can display a `SELECT`
can display it.

### Milestone 7 — the buffer pool

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/databases/{db}/buffer-pool` | every frame, plus hit and miss counters |

```json
{
  "capacity": 128, "page_size": 4096, "resident": 128, "dirty": 0,
  "bytes_used": 524288,
  "frames": [
    { "frame_id": 0, "page_id": 7, "dirty": false, "reads": 5, "writes": 0,
      "recency": 0, "resident_for_ns": 1200000 },
    { "frame_id": 1, "page_id": null, "dirty": false, "reads": 0, "writes": 0,
      "recency": -1, "resident_for_ns": 0 }
  ],
  "stats": { "hits": 2901, "misses": 0, "lookups": 2901, "hit_rate": 1.0,
             "evictions": 8, "dirty_evictions": 2, "writes_absorbed": 3026,
             "flushes": 1, "pages_flushed": 143 },
  "logical_reads": 2901, "physical_reads": 0,
  "logical_writes": 3169, "physical_writes": 143
}
```

**Free frames are reported too.** The grid has a fixed shape, so a page
appearing in a slot reads as a change rather than as the whole layout reflowing.
`recency` is `0` for the most recently used and `-1` for a free frame; the
highest rank among resident frames is what eviction takes next.

There is no `pin_count` — see the event schema for why.

The gap between `logical_reads` and `physical_reads` is the pool working. Those
two counters are cumulative for the open handle, not for the pool, so they
survive a `clear()`.

### Milestone 8 — transactions

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/databases/{db}/transactions` | the open transaction, its undo log, and finished history |
| `POST` | `/databases/{db}/transactions` | `BEGIN` |
| `POST` | `/databases/{db}/transactions/commit` | `COMMIT` |
| `POST` | `/databases/{db}/transactions/rollback` | `ROLLBACK` |

```json
{
  "active": {
    "transaction_id": 12, "state": "active", "implicit": false,
    "statements": 3, "pages_written": 41, "pages_held": 4,
    "pages_restored": 0, "undo_bytes": 16384, "duration_ns": 8100000,
    "records": [
      { "sequence": 0, "page_id": 4, "before_image_size": 4096,
        "reason": "heap insert" }
    ]
  },
  "history": [
    { "transaction_id": 11, "state": "committed", "implicit": true,
      "statements": 1, "pages_written": 3, "pages_held": 0,
      "pages_restored": 0, "undo_bytes": 0, "duration_ns": 210000,
      "records": [] }
  ],
  "history_limit": 50,
  "in_transaction": true, "is_failed": false, "in_explicit_transaction": true,
  "undo_bytes": 16384
}
```

**`records` is the active transaction only.** A finished one has released its
undo log, so the field is always `[]` in `history` — reporting a stale copy
would suggest a rollback was still possible.

**`pages_held` is the number that matters.** It grows with distinct pages
touched, not with rows written, which is what makes whole-page snapshots
affordable. The gap between it and `pages_written` is first-write-wins doing its
job, and the explorer renders exactly that comparison.

**`state` may be `failed`**: open, but doomed. A statement in it raised, so only
`COMMIT` and `ROLLBACK` are accepted and anything else is a 422 reading
`current transaction is aborted, commands ignored until end of transaction
block`. PostgreSQL's rule and PostgreSQL's wording.

**`COMMIT` on a failed transaction rolls back**, and the response says so:
`action` is `"rollback"`, not `"commit"`. `action` reports the *outcome*, since
a caller that asked to commit and got a rollback needs telling in the field it
switches on rather than only in prose it might not render.

The three verbs exist as endpoints as well as SQL because the explorer's panel
has buttons, and a button that secretly submitted `BEGIN;` through the query
endpoint would make the query history lie about what the user ran. They are the
same three manager calls either way, and either route can end a transaction the
other one opened.

`POST` rather than `PUT`: `BEGIN` is not idempotent. Sending it twice is a 422,
because ChenDB has no savepoints and will not pretend to nest.

**Statelessness.** HTTP has no session, so an explicit transaction opened by one
request stays open across requests until some later request ends it — the state
lives on the database handle. That is a footgun for a multi-client server, and
it is acceptable here for the reason the whole workspace design is: this API
serves one explorer looking at its own file. `GET` reports the open transaction
on every call so the UI can show a persistent banner rather than letting one be
forgotten.

### Milestone 9 — the write-ahead log

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/databases/{db}/wal` | the records, the LSNs, and what fsync costs |
| `GET` | `/databases/{db}/recovery` | what the last open had to repair |
| `POST` | `/databases/{db}/checkpoint` | flush every dirty page, discard the log |
| `POST` | `/databases/{db}/crash` | **destructive** — abandon the handle, then recover |

```json
{
  "enabled": true, "path": "demo.chendb-wal",
  "base_lsn": 0, "next_lsn": 281600, "flushed_lsn": 281600,
  "buffered_bytes": 0, "size_bytes": 281600,
  "records": [
    { "lsn": 556, "prev_lsn": 0, "transaction_id": 1, "record_type": "update",
      "page_id": 4, "size": 1068, "before_image_size": 512,
      "after_image_size": 512 },
    { "lsn": 1624, "prev_lsn": 556, "transaction_id": 1,
      "record_type": "commit", "page_id": 0, "size": 44,
      "before_image_size": 0, "after_image_size": 0 }
  ],
  "truncated_tail": false, "total_records": 66,
  "stats": { "records_appended": 516, "records_coalesced": 22000,
             "bytes_appended": 2203136, "flushes": 12, "syncs": 6,
             "mean_sync_ns": 50123.0, "checkpoints": 0, "bytes_reclaimed": 0 }
}
```

**Page images are not sent.** A record carries up to two of them, so a thousand
records is eight megabytes of base64 that no panel renders. The *sizes* go out
instead, because the sizes are the interesting part — and a non-zero
`before_image_size` marks the transaction's first write to that page, which is
first-write-wins visible on the wire.

**`records` is a window.** `total_records` says how many there really are, so a
view can say "the last 200 of 12,000" rather than implying it has them all.

**`mean_sync_ns` is the number to look at.** One second divided by it is the
hard ceiling on commits per second, and it has nothing in it about how much work
each transaction did. That is what group commit exists to amortise.

**`records_coalesced`** counts appends that replaced a staged record for the same
page instead of following it. In a bulk insert it is ~98% of them; see
`docs/milestone-09-wal.md` for why that is the difference between a 197× and a
5× log.

`/checkpoint` is refused with a 422 while a transaction is open: truncating the
log would discard the before-images that transaction needs to roll back.

### `POST /crash`

**This endpoint destroys uncommitted work. That is what it is for.**

It drops the database handle without flushing dirty pages, running a checkpoint,
or rolling anything back — and the next request reopens the file, which runs
recovery. It exists because the alternative is a recovery panel that *describes*
recovery, and a reader has no reason to believe a description.

```json
{
  "message": "handle abandoned without flushing; recovery recovered 66 record(s): 0 redone, 63 already current, 1 undone; 3 finished, 1 interrupted. 50 uncommitted row(s) did not survive.",
  "recovered": { "ran": true, "losers": [4], "pages_undone": 1, "…": "…" },
  "rows_before": { "users": 53 },
  "rows_after": { "users": 3 }
}
```

The response reports recovery as *already done*, not as pending: reopening is
what triggers it, so reporting the pre-crash state and letting a later poll show
the truth would make the response a lie. The row counts are gathered on both
sides by the endpoint rather than left to the caller, because a caller that
forgot to ask first would have nothing to compare against.

What it can lose is exactly what a power cut would lose. It deletes no files, it
is scoped to one database in the workspace the server was pointed at, and it
cannot touch anything the engine promised to keep — every committed row survives,
because its commit record was `fsync`ed when it committed.

Arriving later: `/locks` (10).

## Errors

Every non-2xx response carries the same envelope:

```json
{ "detail": { "error": "DatabaseNotFound", "message": "no database 'x' in this workspace" } }
```

| Status | When |
|---|---|
| 400 | malformed database id or page id |
| 404 | unknown database, page or table |
| 409 | database already exists; second table (Milestone 1 allows one) |
| 422 | validation failure — bad type, NOT NULL violation, wrong arity, unknown field |
| 500 | unmapped engine error, i.e. a bug |

Request bodies set `extra="forbid"`, so a typo'd field is a 422 rather than a
silently ignored value.

---

## Selected endpoints

### `GET /health`

```json
{
  "engine_version": "0.1.0",
  "api_version": "v1",
  "milestone": 1,
  "workspace": "workspace",
  "open_databases": 1,
  "features": {
    "storage": true, "page_inspector": true, "diagnostics": true,
    "event_stream": true, "sql": false, "execution": false,
    "catalog": false, "indexes": false, "planner": false,
    "buffer_pool": false, "transactions": false, "wal": false, "mvcc": false
  }
}
```

`workspace` is a directory *name*, never a path. Doubles as a liveness probe —
the visualizer polls it every 5 s.

### `POST /databases`

```json
{ "database_id": "demo", "page_size": 256 }
```

`page_size` must be a power of two in [256, 65536]. Small pages fill after a
few rows, which makes page chaining easy to watch.

### `POST /databases/{db}/table`

```json
{
  "name": "users",
  "columns": [
    { "name": "id",    "type": "INTEGER", "nullable": false, "primary_key": true },
    { "name": "email", "type": "TEXT",    "nullable": false },
    { "name": "age",   "type": "INTEGER" }
  ]
}
```

409 if a table already exists — Milestone 1 allows one per database.

### `GET /databases/{db}/records`

```json
{
  "columns": [ { "name": "id", "type": "INTEGER", "nullable": false,
                 "primary_key": true, "fixed_size": 8 } ],
  "rows": [ { "record_id": { "page_id": 3, "slot_id": 0 },
              "values": [1, "ada@example.com", 36] } ],
  "offset": 0, "limit": 100, "returned": 1, "has_more": false,
  "rows_scanned": 1, "pages_read": 1, "duration_ns": 180700
}
```

`rows_scanned`, `pages_read` and `duration_ns` are the cost of the request, not
decoration. There is no index and no `LIMIT` pushdown yet, so `offset` is
honoured by discarding rows the scan already produced — the response says so
rather than hiding it.

### `GET /databases/{db}/pages/{page_id}`

The page inspector's data. Real bytes from the real file.

```json
{
  "summary": {
    "page_id": 3, "page_type": "HEAP", "file_offset": 768,
    "lsn": 0, "checksum": 3465019321, "checksum_valid": true,
    "slot_count": 5, "live_record_count": 5,
    "free_space": 38, "reclaimable_space": 0,
    "next_page_id": 4, "owner": "users", "error": null, "dirty": false
  },
  "header_fields": [
    { "name": "free_end", "offset": 18, "size": 2, "value": 82,
      "raw_hex": "5200", "description": "start of record data (pd_upper)" }
  ],
  "slots": [
    { "slot_id": 0, "offset": 227, "length": 29, "is_live": true,
      "raw_hex": "0401000000000000000f0000006164...",
      "record": {
        "values": [1, "ada@example.com", null, true],
        "fields": [ { "index": 0, "name": "id", "type_name": "INTEGER",
                      "is_null": false, "offset": 1, "length": 8, "value": 1 } ],
        "null_bitmap_hex": "04",
        "null_bitmap_bits": [false, false, true, false],
        "null_bitmap_size": 1, "total_size": 29
      } }
  ],
  "raw_hex": "b9c15bce...",
  "page_size": 256, "header_size": 24,
  "slot_directory_end": 44, "free_start": 44, "free_end": 82
}
```

Notes:

- `error` is set instead of raising when a page fails to decode. A corrupt page
  is exactly what the inspector exists to show.
- `dirty` is always `false` in Milestone 1: with no buffer pool, no page is
  ever cached-and-dirty. The field exists so the shape does not change in
  Milestone 7.
- `raw_hex` is the whole page — `2 × page_size` characters.

### `GET /databases/{db}/events`

`?after_seq=` is a stable cursor: sequence numbers are monotonic, so paging
cannot skip or repeat an event while new ones arrive.

```json
{
  "events": [ { "seq": 147, "timestamp_ns": 1753499288632000000,
                "category": "storage", "level": "STORAGE",
                "event_type": "PageReadEvent",
                "event": { "page_id": 6, "file_offset": 1536,
                           "source": "disk", "duration_ns": 250,
                           "transaction_id": null } } ],
  "stats": { "capacity": 20000, "size": 148, "total_recorded": 148,
             "dropped": 0, "level": "STORAGE" },
  "page": { "after_seq": null, "returned": 1, "next_cursor": 147, "has_more": true }
}
```

`event` is an untyped object rather than a discriminated union of ~40 models:
every event is a flat dataclass, `event_type` already identifies it, and the
frontend renders payloads generically. `event_type` + `category` is the stable
contract; the payload keys come straight from the dataclass fields.

`stats.dropped` counts events evicted from the ring buffer before anyone read
them. Non-zero means the history has gaps.

---

## WebSocket protocol

```
WS /api/v1/databases/{database_id}/events/stream
```

Every frame is a JSON object with a `type` discriminator.

### Lifecycle

```
client                                  server
  │                                        │
  ├── connect ────────────────────────────▶│  accept
  │                                        │  resolve database
  │◀────────────── { "type": "hello" } ────┤  (or error + close 4404)
  │                                        │
  │                                        │  subscribe to the fanout sink
  │◀───────────── { "type": "events" } ────┤  batched, up to 64 per frame
  │◀───────────── { "type": "dropped" } ───┤  when this client fell behind
  │                                        │
  ├── { "type": "set_level", … } ─────────▶│  change verbosity live
  ├── { "type": "ping" } ─────────────────▶│
  │                                        │
  ├── close ──────────────────────────────▶│  unsubscribe (always, in `finally`)
```

### Server → client

```jsonc
{ "type": "hello", "database_id": "demo", "last_seq": 148,
  "trace_level": "STORAGE", "queue_capacity": 512,
  "server_time_ns": 1753499288632000000 }

{ "type": "events", "events": [ /* TraceRecordModel[] */ ] }

{ "type": "dropped", "count": 213, "total_dropped": 213 }

{ "type": "error", "error": "DatabaseNotFound", "message": "…" }
```

`hello.last_seq` lets a client fetch anything older over HTTP; the socket only
carries what happens from now on.

### Client → server

```jsonc
{ "type": "set_level", "level": "VERBOSE" }
{ "type": "ping" }
```

An unrecognised frame gets an `error` reply; the connection stays open.

### Close codes

| Code | Meaning |
|---|---|
| 1000 | normal |
| 4404 | no such database in this workspace |

### Backpressure

Each connection owns a bounded queue (`CHENDB_WS_QUEUE_SIZE`, default 512).
When it is full the **oldest** event is dropped, a counter increments, and the
client is told in a `dropped` frame.

Dropping rather than blocking is the whole point: `Subscription.offer()` runs
on the storage thread that emitted the event and only calls
`loop.call_soon_threadsafe`. It cannot block, so a browser tab that stops
reading can never stall a query inside the engine.
`tests/integration/test_websocket.py` asserts exactly that, and
`tests/unit/test_ws_backpressure.py` pins the drop policy.

### Reconnection

The client reconnects with exponential backoff (500 ms doubling to 10 s). The
gap is visible in the UI rather than papered over.

---

## Security boundaries

**No filesystem paths cross the API.** Clients send a *database id*:

```
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

which rejects `..`, `/`, `\`, leading dots and NUL bytes, plus Windows reserved
device names. The id is joined to the configured workspace root, and the
**resolved** path is re-checked with `Path.is_relative_to` so a symlink planted
inside the workspace cannot redirect a write outside it. The pattern is also
declared on the FastAPI path parameter, so a traversal attempt is rejected
before any filesystem call happens.

Responses never contain absolute paths: `/health` and the database list return
the workspace *directory name* only. `tests/integration/test_api.py` asserts no
response body contains one.

CORS defaults to the Vite dev origins (`http://localhost:5173`,
`http://127.0.0.1:5173`), credentials disabled, methods limited to those the
API uses. Override with `CHENDB_CORS_ORIGINS`.

There is no authentication. This is a local development tool: bind it to
`127.0.0.1` (the default) and do not expose it.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CHENDB_WORKSPACE` | `workspace` | directory holding database files |
| `CHENDB_HOST` | `127.0.0.1` | bind address |
| `CHENDB_PORT` | `8000` | bind port |
| `CHENDB_TRACE_LEVEL` | `STORAGE` | level new databases open at |
| `CHENDB_TRACE_CAPACITY` | `20000` | events retained per database |
| `CHENDB_MAX_OPEN_DATABASES` | `16` | open handles before the oldest is closed |
| `CHENDB_WS_QUEUE_SIZE` | `512` | per-connection event backlog |
| `CHENDB_CORS_ORIGINS` | Vite dev origins | comma-separated |

## Generated TypeScript

```bash
python scripts/generate_api_types.py
```

writes `docs/openapi.json` and `visualizer/src/types/api.ts` from the live
Pydantic models. The output is committed, so the frontend builds without a
running server. Renaming a Pydantic field breaks the TypeScript build instead
of failing at runtime in a browser.
