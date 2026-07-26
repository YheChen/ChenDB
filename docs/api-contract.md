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

Arriving later: `/query` and `/executions/*` (Milestones 2–3),
`/indexes/{name}` (5), `/buffer-pool` (7), `/transactions` (8), `/wal` (9),
`/locks` (10).

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
