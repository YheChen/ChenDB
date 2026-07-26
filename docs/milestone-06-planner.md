# Milestone 6 — the cost-based planner

Milestone 5 built two ways to read a table and chose between them by rule: *use
an index whenever one covers a comparison*. That rule is right below about 14%
selectivity and wrong above it — measurably, by 3.9× on the benchmark. This
milestone replaces the rule with arithmetic.

```
 predicate            rows    seq scan  index scan   M5 chose    M6 chooses
 bucket < 1             20     53.6 ms      0.7 ms   index       index
 bucket < 10           200     53.8 ms      4.6 ms   index       index
 bucket < 50          1000     55.4 ms     22.3 ms   index       index
 bucket < 200         4000     61.4 ms     90.8 ms   index ✗     seq   ✓
 bucket < 700        14000     80.8 ms    310.0 ms   index ✗     seq   ✓
```

Five for five.

---

## What was built

```
engine/planner/
    logical.py      what to compute, with no opinion on how
    statistics.py   what the cost model reasons about
    physical.py     enumerate, cost, choose — and keep the losers

engine/optimizer/
    cost.py         the constants, measured for this engine
    rules.py        rewrites that do not change the answer
```

| | |
|---|---|
| **Statistics** | row/page counts, distinct values, min/max, nulls; gathered lazily, refreshed by `ANALYZE` |
| **Selectivity** | equality by distinct count, ranges by interpolation, `AND`/`OR`/`NOT`/`IS NULL` |
| **Cost model** | constants calibrated against the Milestone 5 benchmark, not copied from PostgreSQL |
| **Rules** | constant folding, filter merging, trivial-filter and identity-projection removal |
| **Planning** | logical plan → rewrites → candidate enumeration → costing → physical plan |
| **SQL** | `EXPLAIN`, `EXPLAIN ANALYZE`, `ANALYZE [table]` |
| **API** | per-operator estimates beside actuals, every alternative with its cost, staleness |
| **Visualizer** | `est` on every operator, "what the planner considered", stale-statistics warning |

---

## The pipeline, and why it has four stages

```
AST ──bind──▶ Logical ──rewrite──▶ Logical' ──enumerate──▶ candidates ──cost──▶ Physical ──▶ Operators
                 │                     │                        │                    │
            no index,            same rows out,           SeqScan and          the cheapest,
           no algorithm          fewer operators          IndexScan            plus the rest
```

Milestones 3–5 collapsed all of this into `build_select_plan`, which went from a
bound statement straight to an operator tree. That works right up until there is
more than one way to run something — at which point you need a form you can
*rewrite* and *compare* before committing to an implementation.

`LogicalScan(users)` says "the rows of users". `PhysicalSeqScan` and
`PhysicalIndexScan` are two ways to get them, with different costs and identical
results. Keeping the first free of the second is what lets a rewrite rule fire
without knowing whether an index exists.

**Physical nodes are data, not operators.** A `PhysicalIndexScan` holds an index
*name* and key bounds, not a `BPlusTree`. `materialise()` turns one into a
running operator as a separate step, which buys three things: a plan can be
costed without opening anything, `EXPLAIN` can print a plan it never runs, and
the API can serialise one outside the engine lock.

---

## Calibration: the interesting part

PostgreSQL's defaults say a page read costs 1.0 and processing a tuple costs
0.01 — CPU a hundred times cheaper than I/O. That is right for compiled code
against a spinning disk. Copying it here would have been wrong by two orders of
magnitude, because in ChenDB:

- a "page read" is a `pread` into the OS page cache plus a **CRC32 over 4 KiB** —
  real work, but microseconds;
- a row costs an interpreted Python `decode_record` plus predicate evaluation —
  and that turns out to be the *dominant* term.

So the constants were **measured**. Setting `PAGE_COST = 1.0` as the unit and
fitting the rest to `benchmarks/index_vs_scan.py`:

```python
PAGE_COST          = 1.0    # pread + CRC32 over the page
RANDOM_PAGE_COST   = 1.0    # no buffer pool yet, so locality is nearly free
CPU_TUPLE_COST     = 0.15   # decode one record — 15x PostgreSQL's ratio
CPU_PREDICATE_COST = 0.05   # evaluate a predicate on an already-decoded row
CPU_INDEX_COST     = 0.005  # compare one key inside a node — a memcmp
```

The fit, measured:

| Plan | Estimated | Measured | µs per unit |
|---|---:|---:|---:|
| index scan, 20 rows | 25 | 0.7 ms | 26.7 |
| index scan, 1 000 rows | 1 168 | 22.2 ms | 19.0 |
| index scan, 4 000 rows | 4 667 | 87.2 ms | 18.7 |
| index scan, 14 000 rows | 16 328 | 307.5 ms | 18.8 |
| sequential scan + filter | 4 303 | 81.3 ms | 18.9 |

Near-constant across a 650× range **and the same for both access paths**. That
second part is what matters: a model that is internally consistent but
mis-weights one path against the other picks the wrong plan while looking
perfectly calibrated. Getting there took one correction — charging a full
`CPU_TUPLE_COST` for predicate evaluation double-counted the decode and
over-costed a filtered sequential scan by 45%, which biased every crossover
toward the index. Splitting out `CPU_PREDICATE_COST` moved the seq row from 12.9
µs/unit to 18.9.

These constants are per-engine and will change. Milestone 7's buffer pool makes
a cached page nearly free and an uncached one genuinely expensive, at which
point `RANDOM_PAGE_COST` starts to mean something and has to be re-measured.
That is normal — PostgreSQL ships `random_page_cost` as a knob precisely because
nobody can know it in advance.

---

## Statistics

Four numbers per column: **distinct count**, **min**, **max**, **null count**.
Enough for the three shapes of predicate the planner sees:

| Predicate | Estimate | Needs |
|---|---|---|
| `col = 5` | `1 / distinct` | distinct count |
| `col < 5` | linear between min and max | min, max |
| `col IS NULL` | `nulls / rows` | null count |

### What is missing, and what it costs

**No histogram.** `1 / distinct` assumes every value is equally common, so an
index on a column where 90% of rows share one value is estimated as highly
selective and chosen catastrophically. PostgreSQL keeps a most-common-values
list *and* a histogram in `pg_statistic` for exactly this. It is the first thing
to add if the estimates start being wrong.

**Independence.** `AND` multiplies. `city = 'Paris' AND country = 'France'` is
estimated as the product of two small numbers when the second is implied by the
first, so the estimate can be off by the correlation. PostgreSQL added
`CREATE STATISTICS` in version 10 to let a DBA say otherwise; nothing here can.

**Uniformity.** A range estimate interpolates linearly between min and max, so
skewed data is estimated wrong by however skewed it is. It still gets the
*shape* right — a bound outside the observed range estimates ~0 or ~1, which a
fixed guess handles worst.

### Why they are not persisted

Not a `chendb_stats` table, and therefore no format version 4:

- a statistic has no correctness consequence — a wrong one produces a slow
  query, never a wrong answer — and the file format should change for things
  that must survive, not for hints;
- the interesting failure here is **staleness**, and making statistics vanish on
  close makes their age impossible to ignore;
- PostgreSQL persists them because rescanning a terabyte at startup is
  impossible. That reason does not apply at this scale, and adopting the
  mechanism without the reason is cargo cult.

Staleness is detected by comparing the database's page-write counter against its
value when `ANALYZE` ran — no hook in the heap, the index or the catalog. Stale
statistics are still *used*, because a slightly old estimate beats none and
recomputing per insert would cost a full scan per row. They are reported instead,
in `EXPLAIN` and in the plan view.

### One trap worth naming

`ANALYZE` and `EXPLAIN ANALYZE` do opposite things. The first gathers
statistics; the second *runs* the query and reports actual rows beside the
estimates. Sharing the word is SQL's fault; both are implemented here, and they
are unrelated.

---

## Rewrite rules

A rule takes a logical plan and returns one that produces **exactly the same
rows**. Four exist:

| Rule | What it does |
|---|---|
| `fold_constants` | `age > 2 * 5` → `age > 10`: once at plan time, not once per row |
| `merge_adjacent_filters` | `Filter(a)` over `Filter(b)` → `Filter(a AND b)` |
| `remove_trivial_filter` | drops `WHERE TRUE`, which folding is what produces |
| `drop_identity_projection` | skips a projection returning every column unchanged |

Only rules that actually changed the tree are reported, which took care: an
early version rebuilt every expression node unconditionally, so `fold_constants`
claimed to fire on every query and the report became noise.

Constant folding turns out to be load-bearing beyond speed. `-100` parses as
`UnaryOp(NEGATE, Literal(100))`, not as a negative literal, so
`WHERE bucket < -100` matches neither the selectivity estimator's
`column <op> literal` shape nor the index planner's — until folding collapses
it. `test_a_negative_literal_is_only_usable_after_folding` pins that down.

The two rules that matter most in a real optimiser are absent for structural
reasons: **predicate pushdown** needs a join to push through, and **join
reordering** needs joins. Join order is where the combinatorics live — *n*
tables admit `(2n−2)!/(n−1)!` left-deep orders, 30,240 for six and 17 billion
for ten, which is why PostgreSQL enumerates exhaustively only below
`geqo_threshold` and switches to a genetic algorithm above it.

---

## EXPLAIN

```sql
EXPLAIN SELECT id FROM users WHERE bucket = 5;
```

```
PhysicalProject  id  (cost=26.3 rows=20)
  └─ PhysicalIndexScan  index=users_bucket bucket = 5  (cost=26.2 rows=20)

Statistics: 2000 rows, 87 pages
Alternatives considered:
     Sequential scan of users  cost=387.0 rows=2000  [14.8x the cost of the chosen plan]
  -> Index scan on users_bucket (bucket = 5)  cost=26.2 rows=20
```

The cost shown is **cumulative** — this node plus everything below it — which is
what PostgreSQL prints and what a comparison actually uses. A per-node figure
would make the root look free.

`EXPLAIN ANALYZE` runs the query and appends the truth:

```
PhysicalIndexScan  index=users_bucket bucket = 5  (cost=26.2 rows=20) (actual rows=20 time=0.20ms)
```

It returns **rows**, not a bespoke response shape, so every client that can
display a `SELECT` can display an `EXPLAIN` — which is why PostgreSQL's returns
a one-column result set too.

---

## Forcing a path

`PlannerOptions(enable_seq_scan=…, enable_index_scan=…)`, the equivalent of
PostgreSQL's `enable_seqscan` / `enable_indexscan`. A disabled path is
**penalised, not removed** — `DISABLE_COST = 1e10`, the same trick PostgreSQL
uses — because a query with every path disabled must still produce a plan.
Turning one off is a strong preference, not a prohibition.

This is how `benchmarks/index_vs_scan.py` can time both paths for the same
query and then grade the planner's actual choice against the winner.

---

## A bug this milestone introduced, and fixed

Planning reads pages — gathering statistics scans the whole table — and from
Milestone 6 it happens *before* the first operator opens. The step controller
turns page reads into checkpoints, so the first dozen steps of every stepped
query suddenly became the planner counting rows.

`StepController.arm()` fixes it: checkpoints are ignored until the operator tree
is about to open. **You step through execution, not through planning**;
`EXPLAIN` is how you inspect the part that is skipped. Cancellation is
deliberately *not* gated — a query cancelled while being planned still has to
stop.

---

## What the visualizer shows

Each operator row now carries `est N` beside `out N`, and flags the gap once it
exceeds 2× in either direction. That gap is where a slow query's explanation
almost always is: a planner that picks a terrible plan has nearly always
mis-estimated the rows, not mis-added the costs.

Below the tree, **what the planner considered** — every candidate with its cost,
the winner marked, and each loser's reason:

```
▶ Sequential scan of users                       681.0
  3,000 row(s) expected · cheapest
○ Index scan on users_age (age >= 18)           3483.0
  3,000 row(s) expected · 5.1x the cost of the chosen plan
```

Then the statistics the estimates came from, with a warning when they are stale.

A planner that shows only its answer is unarguable. One that shows what it
turned down, and by how much, can be checked — and checking it against
`EXPLAIN ANALYZE` is the whole skill.

---

## Try it

```bash
python examples/milestone6_planner.py
```

```bash
python benchmarks/index_vs_scan.py
```

```sql
CREATE TABLE users (id INTEGER PRIMARY KEY, bucket INTEGER, email TEXT NOT NULL);
CREATE INDEX users_bucket ON users (bucket);
ANALYZE users;

EXPLAIN SELECT id FROM users WHERE bucket = 5;     -- IndexScan
EXPLAIN SELECT id FROM users WHERE bucket < 90;    -- SeqScan; the index loses
EXPLAIN ANALYZE SELECT id FROM users WHERE bucket = 5;
```

---

## What Milestone 7 needs from this

- **`RANDOM_PAGE_COST` becomes meaningful.** With a buffer pool a hit is nearly
  free and a miss is a real read, so the constant has to be re-measured and the
  crossover will move — probably a long way, since the index scan's dominant
  term is re-reading heap pages that a pool would keep.
- **The benchmark is the regression test.** `benchmarks/index_vs_scan.py` prints
  µs-per-cost-unit; if the buffer pool lands and that column stops being flat,
  the model needs recalibrating before the planner can be trusted again.
- **Cache hit rates belong in the cost model.** Estimating what fraction of a
  scan will hit the pool is the next real modelling problem, and it is what
  PostgreSQL's `effective_cache_size` exists to approximate.
