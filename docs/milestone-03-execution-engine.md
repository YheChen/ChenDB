# Milestone 3 — Execution engine and operator debugger

**Status: complete.** Engine version 0.3.0.

## Goal

Run the statements Milestone 2 learned to parse, using the volcano iterator
model — then let someone step through a query one operation at a time and watch
a single row travel up the operator tree.

---

## What was built

### Engine — `engine/executor/`

| Module | Responsibility |
|---|---|
| `binder.py` | resolve names against a schema; `ColumnRef` → `BoundColumnRef` |
| `expression.py` | evaluate an expression against a row, with three-valued logic |
| `operators.py` | `SeqScan`, `Filter`, `Project` — `open()` / `next()` / `close()` |
| `controller.py` | pause, step and cancel a running query |
| `engine.py` | statement → plan → `QueryResult` |

Four new events in the `operator` category, plus `POST /query`,
`POST /query/step` and the `/executions/{id}/*` family.

### The pipeline

```
SQL text ──parse──▶ AST ──bind──▶ bound statement ──plan──▶ operator tree
                                                                │
                                                          pull rows
                                                                ▼
                                                          QueryResult
```

### The tree

```
Project(email, age*2)          ← the caller pulls from here
    │  next()
    ▼
Filter(age >= 18)
    │  next()
    ▼
SeqScan(users)                 ← reads pages
    │
    ▼
  heap
```

`Project.next()` calls `Filter.next()`, which calls `SeqScan.next()` until it
finds a row its predicate accepts. Nothing is materialised: at any instant
exactly one row is in flight, which is why `LIMIT 1` over a billion-row table
would read one page.

---

## The demo

Start a stepped execution and press Step repeatedly:

```
 0. operator_open  scan_1     SeqScan opened          ← children open first
 1. operator_open  filter_1   Filter opened
 2. operator_open  project_1  Project opened
 3. operator_next  project_1  Project.next()          ← next() travels DOWN
 4. operator_next  filter_1   Filter.next()
 5. operator_next  scan_1     SeqScan.next()
 6. page_read                 page 3 at offset 768    ← storage does its work
 7. row_emitted    scan_1     (1, 'ada@x.com', 36)    ← rows travel UP
 8. row_emitted    filter_1   (1, 'ada@x.com', 36)
 9. row_emitted    project_1  ('ada@x.com')
10. operator_next  project_1  Project.next()
11. operator_next  filter_1   Filter.next()
12. operator_next  scan_1     SeqScan.next()
13. row_emitted    scan_1     (2, 'alan@x.com', NULL)
14. operator_next  scan_1     SeqScan.next()          ← no filter emit: dropped
```

Steps 13→14 are the whole point. The scan emitted alan's row, and then the scan
was asked for *another* row without the filter emitting anything. The filter
silently dropped it, because `NULL >= 18` is **unknown**, and unknown is not
true. That is required SQL behaviour, and here you can watch it happen.

---

## Three-valued logic

SQL is not boolean logic. `NULL` means *unknown*.

```
NULL = NULL   →  NULL        (not TRUE — this is why IS NULL exists)
NULL = 1      →  NULL        (not FALSE)
1 + NULL      →  NULL

┌──────────────────┬──────────────────┐
│ AND │ T │ F │ N  │  OR │ T │ F │ N  │
├─────┼───┼───┼────┼─────┼───┼───┼────┤
│  T  │ T │ F │ N  │  T  │ T │ T │ T  │
│  F  │ F │ F │ F  │  F  │ T │ F │ N  │
│  N  │ N │ F │ N  │  N  │ T │ N │ N  │
└──────────────────┴──────────────────┘
```

`FALSE AND NULL` is `FALSE`: whatever the unknown turns out to be, the
conjunction cannot be true. Symmetrically `TRUE OR NULL` is `TRUE`. Both tables
are pinned down exhaustively in `tests/unit/test_expression.py`, because getting
them wrong produces *silently wrong answers* rather than errors.

The payoff is one line in `Filter`:

```python
if is_true(verdict):    # only an exact True passes. NULL does not.
    return row
```

Which is why `SELECT count(*) FROM t` and `SELECT count(*) FROM t WHERE x = x`
can legitimately disagree.

`IsNullTest` is a separate AST node, not `BinaryOp(EQ, x, NULL)`. It is the one
construct that can *see* a NULL rather than propagating it, and it always returns
a boolean.

---

## Decisions worth naming

**Explicit `open`/`next`/`close`, not Python generators.** A generator per
operator would be shorter and more idiomatic. The explicit protocol is used
because it *is* the volcano interface (Graefe 1994; PostgreSQL's
`ExecProcNode`), because an operator can be asked for its statistics and state at
any moment where a suspended generator frame cannot, and because pausing between
calls is trivial whereas interrupting a generator mid-`yield` needs `throw()` and
careful cleanup. The cost is a Python method call per row per operator — see
*Where this breaks down*.

**Cancellation raises inside the engine thread**, at the next checkpoint, rather
than killing the thread. Operators then unwind through their own `close()`,
releasing pages and generators exactly as a successful query would. Python
cannot kill a thread anyway, and a thread killed from outside would leak
whatever it held.

**"Run until the next page read" is driven by the diagnostics bus.** The
controller registers itself as a sink; when a `PageReadEvent` arrives it pauses.
The storage engine knows nothing about stepping, and every future "run until X"
mode comes free the moment X emits an event. This is the payoff for Milestone 1's
insistence that events be a general bus rather than logging.

**A single rule-based rewrite.** A projection returning every column unchanged is
dropped from the plan, saving a method call and a tuple build per row.
`SELECT * FROM users` executes as a bare `SeqScan`. That is the seed of
Milestone 6's cost-based optimiser.

**`INSERT` and `CREATE TABLE` have no operator tree.** They call straight into
the storage engine. Modelling a single-row insert as a pipeline would be
structure for its own sake.

**Binding reorders `INSERT` values into schema order** and fills omitted columns
with typed NULL literals, so the executor never has to care which order the
statement named them in. A missing `NOT NULL` column is caught here, with a
source position, before anything is written.

**Division truncates toward zero and raises on zero.** C and PostgreSQL
semantics: `-7 / 2` is `-3`, not Python's `-4`. Division by zero raises rather
than returning NULL (SQLite's choice) because a row quietly vanishing from a
result hides a bug in the query.

**Comparing TEXT with a number is refused.** Python would happily order
`"10" < "9"` lexicographically. SQL requires an explicit cast, and a clear
refusal beats a plausible wrong answer.

---

## Step mode, honestly

A stepped execution holds its database's lock for as long as it is alive. That is
inherent — it is a query suspended mid-operation — and three things keep it from
becoming a hang:

- every step call has a timeout (`CHENDB_STEP_TIMEOUT`, default 10 s);
- an execution untouched for `execution_idle_timeout_seconds` (default 300) is
  cancelled by `ExecutionStore.reap`, called on every store operation rather
  than from a background timer;
- `cancel` deliberately needs no lock — it sets a flag and notifies a condition —
  so it always works, which is the only way to get the lock back.

`tests/integration/test_query_api.py::test_cancelling_releases_the_database_lock`
asserts exactly that: after cancelling, a normal query must succeed immediately.

Modes implemented: `step`, `continue`, `until_row`, `until_page_read`,
`until_operator`. The spec also lists "run until next index operation",
"run until lock wait" and "run until transaction boundary" — those arrive with
Milestones 5, 10 and 8, because the events they stop on do not exist yet. Adding
each is one entry in `_STOPS_AT`.

---

## Complexity

| Operator | Cost for *n* input rows | Memory |
|---|---|---|
| `SeqScan` | O(pages) reads, O(n) rows | O(1) |
| `Filter` | O(n) predicate evaluations, no extra I/O | O(1) |
| `Project` | O(n × projections) evaluations | O(1) |

All three stream. A blocking operator — sort, hash aggregate, hash join — would
need O(n), and none exist yet.

One `next()` on a filter can cost many on its child: a selective predicate over a
big table walks the whole table. That is exactly the cost an index removes, and
`test_one_next_on_a_filter_can_cost_many_on_its_child` measures it.

---

## Where this breaks down

**The volcano model's per-row overhead.** A Python method call per row per
operator. PostgreSQL pays the same cost in C and mitigates it with JIT
expression compilation. DuckDB and modern column stores abandon the model for
*vectorised* execution — passing batches of ~2048 rows instead of one — which
amortises the call overhead over the batch and is the single biggest performance
idea this design gives up. The interface would survive it: `next()` returning a
batch instead of a row.

**No blocking operators**, so no `ORDER BY`, `GROUP BY` or joins. The framework
they slot into now exists.

**No index**, so every `SELECT` is a full scan. Milestone 5.

**Binding is minimal.** One table, resolved against a `Schema` handed in by the
caller. Milestone 4 makes it a catalog lookup; Milestone 6 makes it a proper
front-end pass producing a logical plan.

**No transactions**, so a script that fails half-way leaves earlier statements
applied. Milestone 8.

---

## Tests

135 new Python tests (605 total), 9 new frontend tests (62 total).

| File | Covers |
|---|---|
| `tests/unit/test_expression.py` | both truth tables exhaustively, NULL propagation for every operator, arithmetic and truncation, type refusals, binding |
| `tests/unit/test_operators.py` | the iterator protocol, laziness, filter rejection counting, projection, tree teardown, event ordering, and the full step controller |
| `tests/integration/test_query_api.py` | `/query`, `/query/step`, every resume mode, lock release on cancel, registry bounds, error positions |
| `visualizer/.../PlanTree.test.tsx` | tree rendering, statistics, the active-operator marker |

Three worth singling out.

**`test_operator_events_show_next_going_down_and_rows_coming_up`** asserts the
exact event order — opens leaf-first, `next()` root-to-leaf, `row_emitted`
leaf-to-root. If the volcano protocol were ever implemented backwards, this
fails.

**`test_cancelling_a_paused_query_wakes_it`** guards the worst failure mode: a
paused thread that never notices it was cancelled would hang forever holding the
database.

**`test_a_scan_is_lazy`** asserts one `next()` costs exactly one page read. It is
the property the whole iterator model exists for.

### A bug the tests caught

Cancelling during `plan.open()` escaped its handler, because `open()` sat outside
the `try` block in `_execute_select` — so a query cancelled while its operators
were still opening propagated `QueryCancelledError` to the caller instead of
returning a partial result. Moving `open()` inside the `try` fixed it;
`test_cancelling_unwinds_the_operator_tree` found it.

---

## Acceptance criteria

- [x] `CREATE TABLE`, `INSERT` and `SELECT … WHERE` execute against real storage.
- [x] `SELECT *` expands at bind time and its projection is dropped from the plan.
- [x] Both three-valued truth tables are correct; only exact `TRUE` passes a `WHERE`.
- [x] `NULL` arithmetic and comparison propagate rather than defaulting.
- [x] A scan is lazy: one `next()` reads one page.
- [x] Every operator reports rows in, rows out, `next()` calls and duration.
- [x] The plan tree is returned with actual statistics.
- [x] A query can be stepped one operation at a time, with no sleeps anywhere.
- [x] `next()` travelling down and rows travelling up are both observable.
- [x] A page read is a checkpoint without the pager knowing stepping exists.
- [x] Cancelling unwinds the tree and releases the database lock.
- [x] A cancelled query returns a partial result, not an exception.
- [x] The execution registry is bounded and reaps abandoned executions.
- [x] Binding errors carry the exact source position of the offending identifier.
- [x] Tracing does not change query results at any level.

---

## Next: Milestone 4 — persistent catalog and schema explorer

**Engine.** Real system tables (`chendb_tables`, `chendb_columns`) stored as
ordinary heap tuples, multiple tables per database, and a catalog lookup
replacing the single `Schema` handed to the binder. The Milestone 1 JSON schema
page and the one-table limit both go away.

**Visualizer.** Schema browser, table detail, constraint inspection, storage
statistics per table.

**Demo.** Create several tables, restart the engine, and verify that every
schema and all data persist.
