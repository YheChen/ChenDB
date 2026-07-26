# ChenDB

A relational database engine written from scratch in Python, and a web app that
shows you what it is doing while it does it.

The engine is real: a file of fixed-size pages, slotted heap pages, binary
record encoding, checksums, and a page allocator with a free list. The
visualizer is not a mock — every byte it renders was read back from the actual
file on disk.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ChenDB  M2     database: demo (1.8 KiB)     trace: STORAGE    ● engine  │
├──────────────────────────────────────────────────────────────────────────┤
│  [ Storage ]  [ SQL ]                                                    │
├──────────────────────┬───────────────────────┬───────────────────────────┤
│  SCHEMA              │  DISK MAP             │  PAGE INSPECTOR           │
│  users · 16 rows     │  0  META    meta      │  HEAP page 3  checksum ok │
│  # column   type     │  1  SCHEMA  schema    │  ┌────┬──┬─────────┬────┐ │
│  0 id       INTEGER  │  2  SCHEMA  schema    │  │hdr │sl│  free   │recs│ │
│  1 email    TEXT     │  3  HEAP    users ███ │  └────┴──┴─────────┴────┘ │
│  2 age      INTEGER  │  4  HEAP    users ███ │  slot 0  offset 227 len 29│
├──────────────────────┤  5  HEAP    users ███ │    id=1  email=ada@…      │
│  ROWS                │  6  HEAP    users █   │    null bitmap 0x04 = 0010│
│  (3,0) 1 ada@…   36  │                       │                           │
│  (3,1) 2 alan@…  NULL│                       │                           │
├──────────────────────┴───────────────────────┴───────────────────────────┤
│  EVENT TIMELINE                                            live  ▮▮  ⨯    │
│  #156  storage  PageRead    page_id=3 file_offset=768 source=disk 208 ns  │
│  #148  record   HeapScan    action=finished rows_emitted=16 pages=4       │
└──────────────────────────────────────────────────────────────────────────┘
```

**Milestones 1–2 of 10 are complete.** Nothing is stubbed ahead of time: a
feature absent from the engine is absent from the API and hidden in the UI. See
[the roadmap](docs/roadmap.md).

---

## Quick start

```bash
git clone https://github.com/YheChen/ChenDB.git && cd ChenDB
```

### The engine alone — no dependencies

```bash
python -m engine demo.chendb
```

```
ChenDB 0.2.0 — Milestone 2 (storage engine + SQL parser)
chendb> .create users id:INTEGER* email:TEXT! age:INTEGER
created table users with 3 column(s)
chendb> .insert 1 | ada@example.com | 36
inserted at (2,0)
chendb> .page 2
```

`.help` lists the commands. The engine imports with zero third-party packages
installed — a constraint enforced by a test, not a promise.

### Embedded

```python
from engine import Column, DataType, Database, Schema

with Database.open("shop.chendb") as db:
    db.create_table("users", Schema.of(
        Column("id", DataType.INTEGER, nullable=False, primary_key=True),
        Column("email", DataType.TEXT, nullable=False),
    ))
    db.insert((1, "ada@example.com"))

    for record_id, row in db.scan():
        print(record_id, row)          # (2,0) (1, 'ada@example.com')
```

### With the visualizer

```bash
python -m venv .venv && .venv/bin/pip install -e '.[server,dev]'
npm --prefix visualizer install
```

```bash
.venv/bin/python -m engine.server --workspace workspace
```

```bash
npm --prefix visualizer run dev
```

Then open <http://localhost:5173>. Create a database with **256-byte pages** —
a handful of rows will fill a page and you can watch the heap chain grow.

---

## What is actually built

| | |
|---|---|
| **Storage** | fixed-size pages · slotted layout · CRC32 per page · page allocator with a free list · compaction |
| **Records** | INTEGER / FLOAT / BOOLEAN / TEXT · null bitmap · schema-driven encode and decode |
| **Heap** | linked page chain · O(1) append · lazy scan · tombstone deletes |
| **Persistence** | survives process death; `SIGKILL` tests prove it |
| **Diagnostics** | 5 trace levels · 16 event types · bounded retention · provably result-neutral |
| **SQL front end** | hand-written tokenizer · recursive-descent parser · AST where every node records its source span |
| **API** | versioned HTTP + WebSocket · generated TypeScript types · path containment |
| **Visualizer** | disk map · page inspector (layout / header / slots / hex) · Monaco SQL editor · token stream · AST tree with two-way source highlighting · live event timeline |

Not yet built: query **execution**, a persistent catalog, indexes, a buffer
pool, transactions, WAL, MVCC. Those are Milestones 3 through 10. Parsing is not
executing, so there is a `POST /parse` and deliberately no `POST /query`.

---

## Two bits worth looking at

**The AST knows where it came from.** Click any node and the editor highlights
the exact SQL that produced it:

```
SELECT email, age * 2 AS doubled FROM users WHERE age >= 18 AND email IS NOT NULL

SelectStatement                    SELECT email, age * 2 AS doubled FROM users …
└─ SelectItem doubled              age * 2 AS doubled
   └─ BinaryOp        *            age * 2
      └─ ColumnRef    age          age
      └─ Literal      2            2
└─ BinaryOp           AND          age >= 18 AND email IS NOT NULL
   └─ BinaryOp        >=           age >= 18
   └─ IsNullTest      IS NOT NULL  email IS NOT NULL
```

`AND` sits above `>=` because precedence is encoded in the *shape* of the
grammar rules, not a table. And `IS NOT NULL` is its own node, never an
equality against `NULL` — those mean different things in three-valued logic.

**The page inspector shows real bytes.** Select a slot and the Hex tab
highlights exactly the range it points at:

```
00000300  b9 c1 5b ce 00 00 00 00  00 00 00 00 02 00 05 00   ← header
00000310  2c 00 52 00 04 00 00 00  e3 00 1d 00 bd 00 26 00   ← slot directory
00000320  96 00 27 00 6f 00 27 00  52 00 1d 00 __ __ __ __
...
000003f0  61 64 61 40 65 78 61 6d  70 6c 65 2e 63 6f 6d 01   ← record data
```

and the decoded view next to it:

```
slot 0  offset 227  len 29
  id=1   email=ada@example.com   age=NULL   active=true
  null bitmap 0x04 = 0010  ·  29 bytes total
```

`0x04` is bit 2 — `age` is NULL, and it occupies no bytes at all. The full
layout is documented in [docs/storage-format.md](docs/storage-format.md).

---

## Architecture

```
visualizer (TS) ──HTTP/WS──▶ engine.server ──imports──▶ engine.* (stdlib only)
                                   │                          │
                                   └── mappers.py ────────────┘
```

Four rules, enforced by `tests/unit/test_architecture_boundaries.py`:

1. Nothing under `engine/` except `engine/server/` may import a web framework.
2. Nothing under `engine/` except `engine/server/` may import anything outside
   the standard library.
3. `engine/server/` imports `engine`, never the reverse.
4. Diagnostic events cannot serialize themselves — that happens only at the
   boundary.

The result is an engine you can embed, test and reason about with no web
concerns anywhere near it. [docs/architecture.md](docs/architecture.md) has the
full picture.

---

## Tests

```bash
.venv/bin/python -m pytest              # 470 tests
npm --prefix visualizer test            # 53 tests
```

Two are worth singling out.

**Crash tests `SIGKILL` a child process.** No `close()`, no `fsync`, no atexit
hooks — any cooperative shutdown would quietly flush the very buffers whose
loss is under test.

**Tracing tests compare file bytes.** The same workload at `OFF`, `STORAGE` and
`VERBOSE` must produce byte-identical database files. Observation does not
change the system observed.

---

## Commands

| | |
|---|---|
| `python -m engine [FILE]` | interactive storage explorer |
| `python -m engine.server` | HTTP + WebSocket API on `127.0.0.1:8000` |
| `npm --prefix visualizer run dev` | visualizer on `localhost:5173` |
| `python -m pytest` | engine, API and recovery tests |
| `npm --prefix visualizer test` | frontend component tests |
| `python benchmarks/trace_overhead.py` | measure diagnostics overhead |
| `python examples/milestone1_storage.py` | narrated walkthrough of the storage engine |
| `python examples/milestone2_parser.py` | narrated walkthrough of the SQL front end |
| `python scripts/generate_api_types.py` | regenerate TypeScript from OpenAPI |
| `make help` | all of the above |

---

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | layers, boundaries, an insert end to end |
| [Storage format](docs/storage-format.md) | every byte on disk, and why |
| [Event schema](docs/event-schema.md) | the diagnostics contract |
| [API contract](docs/api-contract.md) | HTTP, WebSocket, security boundaries |
| [Milestone 1](docs/milestone-01-storage-engine.md) | storage: what shipped, measured, and what it cannot do |
| [Milestone 2](docs/milestone-02-sql-parser.md) | the SQL front end, the grammar, and a bug worth recording |
| [Roadmap](docs/roadmap.md) | Milestones 2–10 |
| [Performance](docs/performance.md) | where the time goes |
| [Instrumenting a component](docs/how-to-instrument.md) | adding events |
| [Adding a panel](docs/how-to-add-a-panel.md) | engine → API → UI |

## Requirements

Python 3.13+. Node 20+ for the visualizer. The engine itself has no runtime
dependencies; FastAPI, Pydantic and uvicorn are needed only by the server.
