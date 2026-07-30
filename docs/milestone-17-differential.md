# Milestone 17 — Differential testing against SQLite

Every one of the 1,243 tests before this milestone was written by whoever wrote
the engine. So between them they test what its author thought of, and the
project's own record says that is not enough:

| Milestone | What was wrong | How it was found |
|---|---|---|
| 11 | four bugs in MVCC, the WAL, and the catalog's row count | by accident, while building something else |
| 12 | a stale demo button that had shipped broken for a milestone | CI's first run, the day CI existed |
| 12–13 | six user-facing strings claiming shipped features were absent | a screenshot the user sent |
| 16 | two bugs in the WASM build | trying it in a browser |

None was found by a test aimed at it, because **you have to think of the query
before you can write the assertion.** A second engine does not have to think of
anything. It just answers, and the two answers either match or they do not.

On its first real run this one found seven bugs. Two of them were silent wrong
answers — the worst kind, because nothing looks broken.

---

## What was built

| File | What it does |
|---|---|
| `tests/differential/generator.py` | random schemas, rows and *typed* queries, from a seed |
| `tests/differential/dialect.py` | notation and representation, and nothing else |
| `tests/differential/engines.py` | the two adapters, behind one `Outcome` |
| `tests/differential/oracle.py` | whether two answers are the same answer |
| `tests/differential/registry.py` | differences that are not bugs, written as rules |
| `tests/differential/shrink.py` | the smallest case that still fails the *same way* |
| `tests/differential/campaign.py` | run, count, report, and render a failure |
| `scripts/differential.py` | long local runs, outside pytest |

Sixty-four seeds × sixteen queries ≈ 1,000 query pairs in CI, in about a second.
`scripts/differential.py --seeds 0:10000` is 160,000 pairs in under two minutes.

---

## The bugs

Seven, in the order they were found. Every one of the first five is a *silent*
disagreement or an outright crash — none would have failed a single existing test.

### 1. A non-boolean `WHERE` matched nothing, and said nothing

```sql
SELECT COUNT(*) FROM t WHERE v;      -- v is INTEGER
```

ChenDB returned `0`. SQLite returns `3`.

`Filter` asked `is_true(verdict)`, `is_true` was `value is True`, and `5 is True`
is `False`. So a value that was never a condition was indistinguishable from a row
that failed one. The same silence dropped every group from `HAVING SUM(v)`.

The sharpest part: **a test existed and agreed with the bug.**

```python
assert is_true(1) is False, "a truthy non-boolean must not pass"
```

That comment is exactly right — Python's truthiness must not leak into SQL, or
`WHERE name` would keep every row with a non-empty name. It stopped one step
short. The rule it wanted is stronger than the one it wrote down: a value that is
not a boolean is not a condition *at all*. It is now an error, at bind time where
the type is known and at evaluation time where it is not.

### 2. `SUM` over `TEXT` returned a concatenation

```sql
SELECT SUM(s) FROM t;    -- ('abd',)   with the result column typed TEXT
```

`_Accumulator.add` did `self._sum + value` and never asked what it was adding.
Python's `+` concatenates strings, so the engine reported a string as the sum of a
column — and `_aggregate_type` dutifully labelled it TEXT.

### 3. `AVG` over `TEXT` leaked a raw `TypeError`

`str / int`, straight out of the executor. Not a `ChenDBError`, so it escaped the
error envelope entirely: a 500 rather than a 400, and invisible to any caller
following the documented contract.

### 4. `SUM` over `BOOLEAN` changed Python type with the row count

`True` over one row, `2` over two, declared `BOOLEAN` either way — and
`SUM(b) > 0` was then refused for comparing a BOOLEAN to a number, one line of
SQL away from the value that had just been produced.

All three are one fix: `SUM` and `AVG` take numbers. `MIN`/`MAX` deliberately
still take anything, because they only ever compare and return an input, so they
are total on every type ChenDB has.

### 5. `INTEGER` meant int64 in storage but not in an expression

```sql
SELECT n + 1 FROM t;     -- 9223372036854775808, under a column labelled INTEGER
```

A value the same engine refuses to store. The codec had always enforced the range
and the evaluator never had; Python's unbounded integers are both why it was
possible and why the check is needed. Now `check_numeric_range` guards arithmetic,
unary minus, division and `SUM` — and the same function refuses a non-finite
float, for reason 6.

### 6. NaN and infinity broke `ORDER BY`, `MIN`/`MAX`, and index scans at once

```sql
SELECT a FROM f ORDER BY a;    -- (2.0), (nan), (1.0), (inf)
```

Genuinely unsorted. IEEE comparison is a *partial* order and every layer above the
codec assumes a total one: Python's sort compares with `<` and every comparison
against NaN is false, so nothing displaces anything. `MIN`/`MAX` seeded themselves
with the first value and kept it. Worst of all, `engine/index/key.py` orders NaN
*above* `+inf` by its bit pattern while the evaluator says `NaN > 398.0` is false —
so an index scan and a sequential scan returned **different rows for the same
predicate**.

`FLOAT` now means a *finite* double, the way `INTEGER` already meant exactly
int64. PostgreSQL instead defines a total order over them, which is the more
complete answer and four coordinated changes (comparison, sort, the aggregates,
the key encoding) rather than one. SQLite converts a non-finite result to NULL on
store, which is a third answer and the only one that loses data silently.

### 7. Adding an index changed the answer, twice over

```sql
SELECT f FROM t WHERE f = 9223372036854775807;   -- seq scan: no rows
                                                 -- index scan: one row
SELECT f FROM t WHERE f > 9223372036854775807;   -- seq scan: one row
                                                 -- index scan: no rows
```

Both inverted. `encode_key(value, FLOAT)` does `float(value)`, which rounds that
literal *up* to 2⁶³ — and `_bounds_for` then marked the predicate `absorbed`, so
the exact comparison never ran. An index must never change the result of a query.
A bound is now only absorbed when the key encoding round-trips exactly.

### And one the primary-key fix exposed

Closing the `PRIMARY KEY` gap (below) gave `pid` an index for the first time, and
that made a latent planner bug reachable:

```sql
SELECT * FROM t WHERE id = 3 AND id = 2;    -- returned the row with id = 2
```

`_bounds_for` folded two equalities by *assigning* `low = high = key` rather than
intersecting, and marked both conjuncts absorbed — so `id = 3` was dropped. An
unsatisfiable predicate is not a curiosity: it is what a generated query produces
constantly, and what a query built by string concatenation produces by accident.
Equalities are now intersected, so `id = 3 AND id = 2` gives `low=3, high=2`: an
empty range, which the B+ tree already handles.

### The gap that was already written down

`PRIMARY KEY` **was not enforced**. Two rows with the same key, no complaint.

This one was not a discovery — it is recorded as a known limitation in
`docs/milestone-08-transactions.md` ("`PRIMARY KEY` here is metadata, not a unique
index") *and* in `docs/milestone-11-dml.md`. What nobody had connected is that
unique indexes have existed and worked since Milestone 5. The machinery was
sitting there; nothing pointed the constraint at it.

`Catalog.create_table` now creates `<table>_pkey`, a real unique index, whenever
the schema has a primary key. It is deliberately visible — it appears in the index
list, in the plan view, and in `EXPLAIN` — because it costs real pages, and hiding
it would make a primary key look free and the page count unexplained.

That is a capability the engine did not have, which is why Milestone 17 appears in
`MILESTONE_FEATURES` rather than in `MILESTONES_WITHOUT_ENGINE_FEATURES` beside CI.

---

## The three hard parts

### Narrow domains, not wide ones

The instinct for a fuzzer is a wide value domain, and for a database it is exactly
wrong. With integers drawn from the whole 64-bit range, no join ever matches, every
`GROUP BY` makes one group per row, and a hundred thousand cases exercise one code
path.

So the domains are tiny — seven integers, five strings, five floats — which makes
duplicate keys, multi-row groups, empty groups and unmatched rows the *common*
case. It also puts int64 overflow out of reach by construction rather than by
filtering, so bug 5 above had to be found by hand and then pinned as a registry
entry.

The schema is shaped the same way. `child.parent_id` is drawn from three pools —
keys the parent has (60%), keys it has not (25%), and NULL (15%) — so one join has
matched rows, orphans and unknown-keyed rows at once. That is the difference
between exercising a join and merely calling one, and it is what Milestone 18's
outer joins will need.

### A generated query whose answer is not defined cannot be compared

This is the constraint everything else bends around, and the traps are not
obvious.

`ORDER BY` over a non-unique key leaves tied rows in an unspecified order. It is
generated freely anyway, because that is exactly where a NULL-ordering bug lives,
and three things about it *are* defined: the sequence of sort-key values, the
multiset of rows, and — the one that matters — the multiset of rows **within each
run of equal keys**. Without that third clause a sort that carries a row across a
tie boundary satisfies the first two and passes. It is four lines of
`itertools.groupby`.

`LIMIT` without a total order picks an unspecified *subset*, which no comparison
can rescue, so it is gated on a computed `total_order` flag. Computing that flag
is where the harness got it wrong first: a primary key is unique **in its own
table**, not in a join's output, so a self-join ordered by `b.pid` was called
total and the tester reported two perfectly legal tie orders as a divergence. A
total order over a join needs a unique non-null key from *every* source.

Two structural decisions carry most of the weight:

* **Every projection is aliased `c0`, `c1`, …** One line, three payoffs: column
  names match between the engines (unaliased they diverge by design — ChenDB says
  `avg(parent.p_int)`, SQLite says `AVG(p_int)`), `ORDER BY c0` is legal in both,
  and ChenDB's rule that a sort key must appear in the select list stops being a
  restriction to work around.
* **The expression builder is typed.** It is asked for a type and returns only
  that type. Not tidiness — ChenDB refuses `s > 1`, `s + 1` and `WHERE i` where
  SQLite coerces all three, so an untyped builder would emit thousands of
  one-sided errors and drown the signal in its own noise.

### Where NULLs sort, and what a translation is allowed to do

ChenDB puts NULLs last in `ASC` and first in `DESC` — PostgreSQL's default, and the
opposite of SQLite's. The standard leaves it open.

The tempting response is to stop generating `ORDER BY` over nullable columns, which
would give up the corner most likely to hide a bug. Instead SQLite is *asked* for
ChenDB's order, with the `NULLS LAST` / `NULLS FIRST` modifiers it has had since
3.30 — and the generated SQL says so out loud, because a failing case is read by a
human.

That sets the rule for the whole dialect layer: **a translation is allowed only
when the difference is notation or representation, never when it is about what the
query means.** It is very easy to write a compatibility layer that quietly repairs
a disagreement instead of reporting it, and a differential tester that does that
is worse than no tester — it is a green tick over a bug.

An earlier draft broke that rule by accident: `to_sqlite_ddl` ran
`str.replace(" FLOAT", " REAL")` over the whole setup script, which would have
corrupted a row whose *value* contained the string `' FLOAT'` on the SQLite side
only, and manufactured a divergence out of nothing. Both dialects are now rendered
from the same spec, so there is no text to get wrong.

---

## The registry, and the line it draws

Legitimate differences have to be recorded somewhere, or the suite is red forever
and gets turned off. That somewhere is also the obvious place to hide a bug, so the
interesting part is the constraints — each enforced by a test, not by discipline:

1. **An entry may excuse an error, never a value.** Exactly one side must have
   erred. There is deliberately nowhere to record "both returned rows and the rows
   differed" — that is what the tester exists to find.
2. **A defensible difference in a value is fixed in the SQL**, either as notation
   (`NULLS LAST`) or by the generator not emitting it (float `%`). Both are visible
   in the SQL a human reads. Registering it would bury it in the oracle.
3. **Entries match on rules, not identity.** `Entry.matches` sees only the two
   outcomes. It cannot see the SQL, the seed, or a case id, so **there is no
   "known failing seeds" list.** That is the whole line: a divergence you can state
   as a rule is knowledge; a seed you excuse is a bug you have not diagnosed.
4. **One error class per entry**, so its reason has to be true of one thing.
5. **A cap of twenty.** Rules 1–4 keep each entry honest; a cap keeps the *list*
   honest, because a register that only grows is an escape hatch however carefully
   each line is worded.

And it cannot rot: every entry carries a minimal example, and a test runs it and
asserts the divergence **still happens**. When ChenDB stops raising on division by
zero, that test goes red and the entry has to go. That is the mechanism the stale
CLI milestone string never had.

`non-boolean-condition-refused` is the entry worth reading twice. Until this
milestone it was a *silent wrong answer*, which is a bug. The fix turned it into an
error, which is a difference. The same construct changed class — which is why an
entry records its reason and not just its shape.

---

## What stops it going quiet

A differential tester that has silently stopped comparing anything is green, fast,
and worthless, and it looks exactly like one that works. `test_harness.py` is the
part this milestone would not ship without:

* **`test_the_oracle_catches_a_planted_difference`** — a planted difference of each
  kind the oracle claims to detect: a value, NULL vs `0`, NULL vs `''`, `2` vs
  `2.0`, a missing row, a duplicate, a column count, a column name. This is the
  only guard that would notice `compare()` having been reduced to `return AGREE`.
* **`test_the_corpus_hits_every_corner`** — eighteen named corners, each ≥ 5 times
  across the CI seeds. A weight is a hope; this is a check. A generator that stops
  emitting self-joins is the same failure as a guard that skips quietly.
* **`test_the_corpus_is_not_trivial`** — ≥ 85% of pairs run on both engines, ≥ 30%
  of `SELECT`s return a row. A corpus where everything errors agrees perfectly.
* **`test_the_canonical_key_never_separates_two_equal_values`** — the invariant
  `-0.0` broke. The multiset comparison sorts by a key and then walks the pairs; a
  key *finer* than the equality it serves compares the wrong pairs and invents a
  divergence. It cost three false accusations before the engine was even suspect.
* **`test_sqlite_is_new_enough`** — **fails** rather than skips. `sqlite3` is in the
  standard library and cannot be absent, so there is nothing to be lenient about.

Both of the first two were verified by watching them fail: weakening `compare()` to
always agree turns seven tests red, and making division by zero return NULL turns
the `division-by-zero-raises` entry red.

---

## Shrinking

A random failure is nearly worthless unshrunk — sixteen queries over two tables
with a dozen columns and eight rows each, when the problem is one query, one
column and two rows.

Two properties make it trustworthy. **It keeps the same failure**: a candidate is
accepted only when the smaller case still produces the identical
`Comparison.signature()`, not merely when it still fails somehow. Without that a
shrinker wanders onto a second, easier bug and reports a beautiful minimal case for
something nobody was looking at. **Every reduction is on the spec**, and the SQL is
re-rendered from it — so a shrunk case is still a case: it runs, it can be shrunk
again, and it pastes into a regression test.

It runs *inside* the failing pytest test, so the minimal case is already in the CI
log and nobody has to re-run anything.

---

## What it costs

| | |
|---|---|
| Fresh ChenDB database + setup | 1.22 ms |
| One query on an open database | 0.16–0.22 ms |
| 64 seeds × 16 queries in CI | ~1 s, 1,024 pairs |
| 10,000 seeds locally | 107 s, 160,000 pairs |

Setup being six times a query is the whole reason a case is **one schema and
sixteen queries** rather than one query. Amortised, the engine is what gets timed
rather than the fixture.

The engine pays for two of the fixes on every row: `is_true` gained an
`isinstance` check, and arithmetic gained a range comparison. Both are in the
measurement noise next to a page miss (1,822 ns).

---

## Try it

```bash
.venv/bin/python -m pytest -q tests/differential           # the CI suite, ~1 s
.venv/bin/python scripts/differential.py --seeds 0:5000    # a real campaign
.venv/bin/python scripts/differential.py --seed 1731 --verbose
.venv/bin/python examples/milestone17_differential.py
```

---

## What is still missing

- **No outer joins in the generator.** That is Milestone 18, and the schema was
  built for it: the child's key column already has orphans and NULLs in it.
- **No `SELECT DISTINCT`, subqueries, `CASE`, `IN`, `LIKE` or `CAST`** — the
  generator can only emit what ChenDB parses, so the intersection of the two
  dialects is bounded by the narrower grammar. Every one of those is a construct
  the tester cannot currently reach.
- **`ORDER BY` may only name something in the select list.** ChenDB sorts *above*
  the projection, so a sort key has to be something the projection produced. Both
  reference engines allow an arbitrary expression. The generator works within it;
  it is a real limitation and a candidate for its own change.
- **`GROUP BY` ordinals are not implemented**, and an integer literal there is
  silently taken as a grouping constant. `ORDER BY 2` *does* work, so the two
  clauses disagree — refusing the literal would be the honest minimum.
- **`UPDATE t SET notnull_col = NULL` is refused at bind time even when no row
  matches**, while `SET notnull_col = v + NULL` is checked per row. The two paths
  disagree about when `NOT NULL` applies; PostgreSQL checks per row.
- **No concurrency, no crashes, no plan comparison.** The tester compares answers.
  Whether two *plans* for the same query agree — the property bug 7 violated — is
  checked only where a generated schema happens to have an index, rather than by
  running every query both ways on purpose.
- **`DEFAULT_MAX_ROWS` silently truncates a `SELECT` at 10,000 rows.** Out of reach
  here, because generated tables have at most eight rows, but a real limit that
  reports itself only through `stats.truncated`.
