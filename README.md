# ChenDB

[![CI](https://github.com/YheChen/ChenDB/actions/workflows/ci.yml/badge.svg)](https://github.com/YheChen/ChenDB/actions/workflows/ci.yml)

A relational database engine written from scratch in Python, and a web app that
shows you what it is doing while it does it.

The engine is real: a file of fixed-size pages, slotted heap pages, binary
record encoding, checksums, a page allocator with a free list, a SQL front end,
a volcano executor, a persistent catalog, disk-backed B+ tree indexes, a
cost-based planner that reorders joins, a buffer pool, transactions with
rollback, a write-ahead log that recovers the database after a crash, and MVCC so
a reader never waits for a writer, where an `UPDATE` leaves the old version of the
row readable rather than overwriting it. The visualizer is not a mock. Every byte
it renders was read back from the actual file on disk.

Its answers are checked against SQLite's: random schemas, random rows and random
queries run through both engines and compared, 320,000 query pairs in four
minutes. That found seven bugs the hand-written tests had missed, and two more
the day its generator learned to build a third table.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ChenDB  M22    database: demo (105 KiB)     trace: STORAGE    ● engine  │
├──────────────────────────────────────────────────────────────────────────┤
│ [Storage][SQL][Execution][Indexes][Buffer][Txns][WAL][MVCC]              │
├──────────────────┬───────────────────────────────────────────────────────┤
│  INDEXES         │  POINT LOOKUP   [ 42            ]  Search             │
│  users_age       │  found yes   matches 13   pages read 4   height 3     │
│   users.age      │  path: p82 → p81 → p67                                │
│   h3 · 600 · 33p ├───────────────────────────────────────────────────────┤
│  users_email     │  B+ TREE   height 3 · 33 nodes                        │
│   users.email    │                    ┌────┬────┐  p82                   │
│  users_pkey      │                    │ -∞ │ 37 │                        │
│   users.id uniq  │                 ┌──┴────┴──┬─┘                        │
│                  │        ┌────┬───▼┬────┐ ┌──▼─┬────┬────┐              │
│                  │        │ -∞ │ 19 │ 35 │ │ -∞ │ 38 │ 61 │              │
│                  │        └────┴────┴────┘ └────┴────┴────┘              │
│                  │   ┌──────┐╌╌▶┌──────┐╌╌▶┌──────┐╌╌▶┌──────┐           │
│                  │   │18 18…│   │19 19…│   │21 21…│   │40 40…│  ← leaves │
│                  │   └──────┘   └──────┘   └──────┘   └──────┘           │
├──────────────────┴───────────────────────────────────────────────────────┤
│  EVENT TIMELINE                                            live  ▮▮  ⨯    │
│  #15500 index    IndexSearch  index_name=users_age key=42 found=true      │
│                               matches=13 pages_visited=4 depth=3  392 µs  │
│  #15499 storage  PageRead     page_id=67 file_offset=34304 disk 333 ns    │
└──────────────────────────────────────────────────────────────────────────┘
```

**Twenty-two milestones are complete.** Nothing is stubbed ahead of time: a
feature absent from the engine is absent from the API and hidden in the UI. See
[the roadmap](docs/roadmap.md).

---

## Quick start

```bash
git clone https://github.com/YheChen/ChenDB.git && cd ChenDB
```

### In a browser: no clone, no install

The visualizer ships with the engine inside it: CPython compiled to
WebAssembly, the same `.py` files, no server anywhere. Every visitor gets a
private database in their own tab, kept in IndexedDB so it survives a refresh.
Deployed to Vercel from `vercel.json`; build it yourself with:

```bash
npm --prefix visualizer run preview:wasm
```

### The engine alone: no dependencies

```bash
python -m engine demo.chendb
```

```
ChenDB 2.2.0 - Milestone 22 (storage + SQL + execution + catalog + indexes + planner + buffer pool + transactions + write-ahead log + MVCC + UPDATE and DELETE + joins and aggregation + enforced primary keys + outer joins + outer-join simplification + skew-aware statistics + reordering across an outer join)
Type .help for commands, .quit to exit. Anything not starting with '.' is SQL.

opened demo.chendb (4 page(s))
chendb> .create users id:INTEGER* email:TEXT! age:INTEGER
created table users with 3 column(s)
chendb> INSERT INTO users VALUES (1,'ada@example.com',36),(2,'alan@example.com',NULL);
inserted 2 row(s) into users
chendb> CREATE INDEX users_age ON users (age);
created index users_age on users(age); 2 entries, height 1
chendb> SELECT email FROM users WHERE age = 36
email
------------
ada@example.com
(1 row(s), 4 page(s) read, 0.66 ms)
Project  email
  └─ Filter  (age = 36)
    └─ SeqScan  table=users
chendb> .indexes
index                  on                     type      h   entries  pages  unique
users_age              users.age              INTEGER   1         2      1  -
users_pkey             users.id               INTEGER   1         2      1  yes
chendb> .tree users_age
root page 6, height 1

leaves:
  p6     [NULL 36]
```

Two things in there are worth a second look, and both are the engine being
honest rather than flattering.

`SELECT ... WHERE age = 36` **does not use the index it just built.** Two rows fit
in one page, so a sequential scan is cheaper than descending a tree and then
fetching from the heap anyway, and the cost model says so. `EXPLAIN` names the
index it turned down and by how much. `benchmarks/index_vs_scan.py` finds the
crossover.

`users_pkey` was never asked for. A `PRIMARY KEY` builds a real unique index,
which is what makes it a constraint rather than a note in the catalog, and it is
listed like any other index because it costs real pages.

`.help` lists the commands: `.tables`, `.schema`, `.indexes`, `.tree`, `.find`,
`.page`, `.hex`, `.events` and the rest. The engine imports with zero
third-party packages installed, a constraint enforced by a test rather than a
promise.

### Embedded

```python
from engine import Column, DataType, Database, Schema

with Database.open("shop.chendb") as db:
    db.create_table("users", Schema.of(
        Column("id", DataType.INTEGER, nullable=False, primary_key=True),
        Column("email", DataType.TEXT, nullable=False),
    ))
    db.insert("users", (1, "ada@example.com"))
    db.create_index("users_email", "users", "email", unique=True)
    print(db.lookup("users_email", "ada@example.com"))

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

Then open <http://localhost:5173>. Create a database with **256-byte pages**.
A handful of rows will fill a page and you can watch the heap chain grow.

---

## What is actually built

| | |
|---|---|
| **Storage** | fixed-size pages · slotted layout · CRC32 per page · page allocator with a free list · compaction |
| **Records** | INTEGER / FLOAT / BOOLEAN / TEXT · null bitmap · schema-driven encode and decode |
| **Heap** | linked page chain · O(1) append · lazy scan · tombstone deletes |
| **Persistence** | survives process death; `SIGKILL` tests prove it |
| **Diagnostics** | 5 trace levels · 47 event types · bounded retention · provably result-neutral |
| **SQL front end** | hand-written tokenizer · recursive-descent parser · AST where every node records its source span |
| **DML** | `INSERT` · `UPDATE ... SET` · `DELETE ... WHERE` · Halloween-safe · `EXPLAIN` on all three |
| **Queries** | inner and outer joins (`LEFT`/`RIGHT`/`FULL`) with aliases and self-joins · hash and nested-loop algorithms · `GROUP BY` / `HAVING` · `COUNT`/`SUM`/`AVG`/`MIN`/`MAX` · `ORDER BY` · `LIMIT`/`OFFSET` |
| **Execution** | volcano operators (scan / filter / project / join / aggregate / sort / limit) · three-valued logic · step-through debugger with real cancellation |
| **Catalog** | many tables per database · system tables stored as heap tuples · schemas rebuilt from disk · a `PRIMARY KEY` builds a real unique index |
| **Indexes** | disk-backed B+ tree · order-preserving key encoding · linked leaves · range scans |
| **Planner** | logical and physical plans · most-common values and equi-depth histograms · a cost model calibrated by measurement · predicate pushdown · System R join-order search · outer joins proved to be inner joins and reordered · `EXPLAIN` |
| **Buffer pool** | write-back · exact LRU · counters for every frame |
| **Transactions** | `BEGIN` / `COMMIT` / `ROLLBACK` · page-level undo log · implicit transactions · atomic DDL |
| **Durability** | write-ahead log · LSN per page · checkpoints · ARIES recovery (analysis / redo / undo) · a crash button that proves it |
| **Concurrency** | row versions (`xmin`/`xmax`) · version chains · snapshot isolation · read committed and repeatable read · row locks · wait-for graph · deadlock detection · manual vacuum |
| **API** | versioned HTTP + WebSocket · generated TypeScript types · path containment |
| **Visualizer** | disk map · page inspector (layout / header / slots / hex) · Monaco SQL editor · token stream · AST tree with two-way source highlighting · live event timeline |
| **Correctness** | 1,699 tests · a seeded generative suite that compares every answer against SQLite · a shrinker · a divergence registry that fails when a listed difference stops diverging |

Five claims worth checking rather than believing:

**A committed transaction survives `SIGKILL` without a sync.** The pages may
still be in memory; the log is enough to put them back. An uncommitted one is
rolled back on the next open. `tests/recovery/` asserts both by killing a real
child process, and the explorer has a crash button that does it live.

**A reader never waits for a writer.** One console holds a row lock; the other
runs a `SELECT` and gets its answer immediately, without the uncommitted row and
without taking a lock of its own. `tests/unit/test_mvcc.py` asserts it and the
MVCC workspace shows it.

**The planner does not join in the order you wrote, unless it may not.** Give
it two tables and it re-derives the order from statistics, builds the hash table
on the smaller side because a build costs more than a probe, and pushes your
`WHERE` below the join. An outer join takes all three away: it is neither
commutative nor associative, so it runs where it was written, its `ON` stays at
the join, and a predicate on the side that can be NULL-extended stays above it.

Then it checks whether the join is really outer. `a LEFT JOIN b ON … WHERE b.x = 5`
is not: `b.x` is NULL in every row the join preserved, `NULL = 5` is NULL, and a
`WHERE` keeps only TRUE, so those rows die anyway. Proving that gives all three
back at once, and on 4,000 rows with an index it is the difference between 19 ms
and half a millisecond. `EXPLAIN` names each decision separately, including that
one. `tests/unit/test_joins.py` asserts it.

**And the numbers it decides on are checked against reality.** Every column
carries its most-common values with exact counts and an equi-depth histogram
over the rest, so a column holding 90% `info`, 9% `warn` and 1% `error` gets
three different estimates rather than one average of them. Where the list covers
every value, which is most columns at this size, the estimate is a count and not
a prediction. `tests/unit/test_estimates.py` runs each case twice, once for what
the planner predicted and once for what came back, which is the test this
project did not have when a join estimate sat 80x under its true value.

**An update does not overwrite anything.** It writes eight bytes to the old
version and appends a new one, so a transaction that took its snapshot first
still reads the old value, and the table panel shows `rows 4 · versions 5`
until you press Vacuum. `tests/unit/test_dml.py` asserts it.

**The answers agree with SQLite's.** Random schemas, random rows and random
queries are run against both engines and compared, 160,000 query pairs in two
minutes, 1,024 of them on every push. It found seven bugs on its first campaign,
two of which returned a wrong answer without any sign of trouble.
`tests/differential/` is the suite and `docs/milestone-17-differential.md` lists
all seven.

Deliberately not built, and each has a paragraph in the milestone docs saying
why: serializable isolation, `RETURNING`, heap-only tuples, bushy plans, index
nested-loop joins, reordering across an outer join that survives simplification,
`USING` and `NATURAL JOIN`, subqueries, `DISTINCT`, parallel statement execution,
lock escalation, autovacuum, and overflow pages for rows larger than a page.

---

## Three bits worth looking at

**You can watch a row move through the plan.** Step a query and the checkpoints
read:

```
operator_next  project_1   Project.next()        ← next() travels DOWN
operator_next  filter_1    Filter.next()
operator_next  scan_1      SeqScan.next()
page_read                  page 3 at offset 768  ← storage does its work
row_emitted    scan_1      (1, 'ada@x.com', 36)  ← rows travel UP
row_emitted    filter_1    (1, 'ada@x.com', 36)
row_emitted    project_1   ('ada@x.com')
...
row_emitted    scan_1      (2, 'alan@x.com', NULL)
operator_next  scan_1      SeqScan.next()        ← no filter emit: dropped
```

Those last two lines are the interesting ones. `NULL >= 18` is *unknown*, and
unknown is not TRUE, so the filter silently drops the row. Required SQL
behaviour, made visible.


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
equality against `NULL`. Those mean different things in three-valued logic.

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

`0x04` is bit 2, `age` is NULL, and it occupies no bytes at all. The full
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
4. Diagnostic events cannot serialize themselves, that happens only at the
   boundary.

The result is an engine you can embed, test and reason about with no web
concerns anywhere near it. [docs/architecture.md](docs/architecture.md) has the
full picture.

---

## Tests

```bash
.venv/bin/python -m pytest              # 1,699 tests
npm --prefix visualizer test            # 160 tests
```

Two are worth singling out.

**Crash tests `SIGKILL` a child process.** No `close()`, no `fsync`, no atexit
hooks, any cooperative shutdown would quietly flush the very buffers whose
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
| `python examples/milestone3_execution.py` | narrated walkthrough of the executor |
| `python examples/milestone4_catalog.py` | narrated walkthrough of the catalog |
| `python examples/milestone5_indexes.py` | narrated walkthrough of the B+ tree |
| `python examples/milestone6_planner.py` | narrated walkthrough of the planner |
| `python examples/milestone7_buffer_pool.py` | narrated walkthrough of the buffer pool |
| `python examples/milestone8_transactions.py` | narrated walkthrough of transactions and rollback |
| `python examples/milestone9_wal.py` | narrated walkthrough of the log, checkpoints and recovery |
| `python examples/milestone10_mvcc.py` | narrated walkthrough of snapshots, locks and deadlocks |
| `python examples/milestone11_dml.py` | narrated walkthrough of `UPDATE`, `DELETE` and the Halloween problem |
| `python examples/milestone13_joins.py` | narrated walkthrough of joins, join order and aggregation |
| `python examples/milestone17_differential.py` | the seven bugs SQLite found, and why a test can agree with one |
| `python examples/milestone18_outer_joins.py` | outer joins, and the licence the planner had to give up |
| `python examples/milestone19_outer_join_simplification.py` | proving an outer join is an inner join, and what that unblocks |
| `python examples/milestone20_skew.py` | one estimate for three queries, and the two structures that fixed it |
| `python scripts/differential.py --seeds 0:5000` | a real differential campaign, ~80,000 query pairs |
| `make ci` | lint, typecheck, both test suites and every example, CI's order |
| `make demo-sql` | every SQL statement the explorer's buttons will produce |
| `python benchmarks/index_vs_scan.py` | where an index wins, and where it loses |
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
| [Milestone 3](docs/milestone-03-execution-engine.md) | the volcano model, three-valued logic, and step mode |
| [Milestone 4](docs/milestone-04-catalog.md) | the catalog bootstrap problem, and format version 2 |
| [Milestone 5](docs/milestone-05-btree-index.md) | order-preserving keys, the B+ tree, and when an index loses |
| [Milestone 6](docs/milestone-06-planner.md) | statistics, a cost model calibrated by measurement, and EXPLAIN |
| [Milestone 7](docs/milestone-07-buffer-pool.md) | the page cache, and why the win was not the syscall |
| [Milestone 8](docs/milestone-08-transactions.md) | physical undo, why it made DDL atomic for free, and where atomicity stops |
| [Milestone 9](docs/milestone-09-wal.md) | write-ahead logging, ARIES recovery, and a 197× log made 5× |
| [Milestone 10](docs/milestone-10-mvcc.md) | row versions, snapshot isolation, deadlocks, and why there is no commit log |
| [Milestone 11](docs/milestone-11-dml.md) | `UPDATE` and `DELETE`, the Halloween problem, and why an update rewrites every index |
| [Milestone 12](docs/milestone-12-ci.md) | CI, and running every demo button against the real engine |
| [Milestone 13](docs/milestone-13-joins.md) | joins, aggregation, and the first decision the cost model could get wrong |
| [Milestone 14](docs/milestone-14-transport.md) | the seam that lets the app carry the engine instead of calling one |
| [Milestone 15](docs/milestone-15-wasm.md) | shipping a database engine as a static file, and the three things that broke |
| [Milestone 16](docs/milestone-16-persistence.md) | keeping a database in IndexedDB, and the escape hatch persistence needs |
| [Milestone 17](docs/milestone-17-differential.md) | seven bugs a second engine found, and the line between a rule and an excuse |
| [Milestone 18](docs/milestone-18-outer-joins.md) | outer joins, why NULL-extension was free, and why the join search needed a barrier |
| [Milestone 19](docs/milestone-19-outer-join-simplification.md) | proving an outer join is an inner join, and the estimate that measurement found broken |
| [Milestone 20](docs/milestone-20-skew.md) | most-common values, equi-depth histograms, and the first test that checks an estimate against reality |
| [Milestone 21](docs/milestone-21-three-tables.md) | a third table in the fuzzer, and the two wrong answers two tables could never find |
| [Milestone 22](docs/milestone-22-reordering.md) | what an outer join actually requires on its left, and the orders that frees |
| [Roadmap](docs/roadmap.md) | every milestone, and what each one added to the file format |
| [Performance](docs/performance.md) | where the time goes |
| [Instrumenting a component](docs/how-to-instrument.md) | adding events |
| [Adding a panel](docs/how-to-add-a-panel.md) | engine → API → UI |

## Requirements

Python 3.13+. Node 20+ for the visualizer. The engine itself has no runtime
dependencies; FastAPI, Pydantic and uvicorn are needed only by the server.
