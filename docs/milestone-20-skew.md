# Milestone 20: Statistics that survive skew

Three queries against the same 20,000-row table:

```sql
SELECT id FROM events WHERE level = 'info';    -- 18,000 rows
SELECT id FROM events WHERE level = 'warn';    --  1,800 rows
SELECT id FROM events WHERE level = 'error';   --    200 rows
```

Before this milestone the planner estimated **6,667 rows for all three**, costed
all three at 5,460, and chose an index scan for all three. It could not tell
apart queries that differ by 90x, because `1 / distinct` says every value is
equally common and `level` has three of them.

That assumption was named as the largest source of error in this cost model
fourteen milestones ago, with the fix written down next to it:

> the equivalent here would be a list of the top *k* values with their
> frequencies, and it is the first thing to add if the estimates start being
> wrong

They started being wrong. Milestone 19 measured its own work and found a
foreign-key join estimated at 80x under its real size, on the most ordinary
join there is.

---

## What was built

| File | What changed |
|---|---|
| `engine/planner/statistics.py` | `most_common`, `histogram`, `summarise`, `STATISTICS_TARGET`; `gather` counts instead of collecting a set |
| `engine/optimizer/cost.py` | `mcv_join_selectivity`; equality, inequality and range estimates read the summary; a one-row floor |
| `engine/planner/physical.py` | `_joined_columns` hands whole columns to the join estimator, not just their distinct counts |
| `tests/unit/test_estimates.py` | new. The first test in this project that compares an estimate against reality |
| `tests/unit/test_cost_model.py` | the selectivity section rewritten against counts |

---

## Two structures, because skew and shape are different problems

The division is PostgreSQL's `pg_statistic`, and it is the right one:
**skew lives in the head of a distribution and shape lives in the tail.**

* **`most_common`** holds the top `STATISTICS_TARGET` values with exact counts.
  A value in the list is not estimated at all.
* **`histogram`** holds equi-depth bucket boundaries over everything *not* in
  that list, so a range predicate counts whole buckets instead of interpolating
  a straight line from min to max.

Equi-depth rather than equi-width is the choice that makes a histogram worth
having. Equal-width buckets over a column that clusters at one end put nearly
every row in one bucket and spend their resolution describing empty space.
Equal-depth buckets spend it where the rows are, which is what a selectivity
estimate is asking about.

### The regime where an estimate stops being one

ChenDB reads every row rather than sampling, which it has done since Milestone 6
and which is affordable at this scale. Combined with an exact count per value it
produces a property a sampling system cannot have:

> When a column has no more distinct values than `STATISTICS_TARGET`, the
> most-common list **is** the column. Equality, inequality and range estimates
> over it are counts, not predictions.

Most columns in a database this size are in that regime. The three-value `level`
column above is; so is every boolean, every status, every enum-shaped column,
and every foreign key into a table of fewer than 32 rows. `covers_every_value`
is the flag, and the estimator uses it to say something no estimate could: that
a value it has never seen occurs zero times.

Except it does not quite say that. See the floor.

### The one-row floor

An absent value is estimated at **one row, never zero**, and this is not
timidity. Every number here is as of the last `ANALYZE`, so "this value does not
occur" means "did not occur when we looked". Predicting zero makes every
operator above the scan free, and a subtree costed at nothing wins every
comparison it is ever part of: no amount of later work can outweigh nothing.
PostgreSQL clamps identically in `clamp_row_est`, for the same reason.

`x = NULL` is deliberately exempt. It admits no rows by the definition of
three-valued logic rather than by observation, which is a claim no future insert
can falsify.

---

## Joins, where the error compounds

A join estimate feeds every join above it, so two tables wrong by 10x make a
four-table plan wrong by 1,000. It is also where uniformity fails hardest,
because the assumption is made twice.

`mcv_join_selectivity` is PostgreSQL's `eqjoinsel_inner` in miniature, three
terms:

1. **Both lists hold the value.** Its contribution is exactly `f₁(v) · f₂(v)`,
   because both frequencies are counts.
2. **One list holds it and the other does not.** It can still match rows in the
   other side's tail, spread over the distinct values that tail holds.
3. **Tail against tail**, uniform. The old estimate, applied to what is left
   rather than to everything.

When both lists are complete, terms 2 and 3 are zero and term 1 is the true join
cardinality over the cross product. Not an estimate.

Measured on two 4,000-row tables that each put 60% of their rows on one user:

| | estimate | actual | |
|---|---|---|---|
| `1 / max(distinct)` | 320,000 | 5,812,254 | **0.06x** |
| matching the two lists | 5,810,130 | 5,812,254 | **1.00x** |

An 18x underestimate is not a rounding error. It is the difference between a
hash join and a nested loop, and every operator above it is costed as if the
join produced a twentieth of what it does.

---

## What it is worth

The three queries from the top, with an index on `level`, 20,000 rows:

| | estimate | actual | plan | time |
|---|---|---|---|---|
| **before** `= 'info'` | 6,667 | 18,000 | index scan | 100.5 ms |
| **before** `= 'warn'` | 6,667 | 1,800 | index scan | 10.7 ms |
| **before** `= 'error'` | 6,667 | 200 | index scan | 1.7 ms |
| **after** `= 'info'` | 18,000 | 18,000 | **sequential scan** | **84.1 ms** |
| **after** `= 'warn'` | 1,800 | 1,800 | index scan | 10.9 ms |
| **after** `= 'error'` | 200 | 200 | index scan | 2.2 ms |

The 1.2x on the hot value is the smaller half of the result. The larger half is
that three identical numbers became three right ones: a planner that gives the
same answer for 200 rows and 18,000 is not making a decision, and everything
above it inherits the same blindness.

---

## Verification

**A new kind of test.** `tests/unit/test_estimates.py` runs each case twice, once
for what the planner predicted and once for what came back, and compares them.
Nothing in this project did that before, which is precisely how a join estimate
80x under its true value passed every test around it for fourteen milestones:
each one checked that the arithmetic was the arithmetic somebody had written
down.

The tolerances are the interesting part. An estimate is not expected to be right,
only right enough to order two plans correctly, so most cases allow a factor of
two. Ten of the eighteen are marked exact, and those are exact because the
column's list is complete.

Two cases are deliberately loose and say why: `level = 'warn' AND code < 10` is
allowed 6x because conjunctions are multiplied as if independent, and they are
not. That is the next thing wrong with this cost model and it is not fixed here.

**Guards watched failing.** Making `summarise` return nothing turns 17 tests red.
Disabling only the MCV join estimator turns exactly the three join cases red.

**320,000 generated query pairs agree with SQLite**, which checks the thing a
cost-model change most needs checking: that it changed no answers. A plan chosen
differently must still be a plan that computes the same rows.

---

## What is still missing

- **Conjunctions are still multiplied as if independent.** `city = 'Paris' AND
  country = 'France'` is estimated as the product of two small numbers when the
  second is implied by the first. PostgreSQL needed `CREATE STATISTICS` and
  multi-column dependencies for this; it is a milestone of its own and the
  largest remaining error.
- **Statistics are still not persisted**, still on purpose. See
  `docs/milestone-06-planner.md`: a wrong statistic costs a slow query and never
  a wrong answer, so the file format should not change for it.
- **The summary is exact, not sampled.** Fine at this scale, and the reason
  `STATISTICS_TARGET` bounds memory rather than sampling error. A table large
  enough to make a full `ANALYZE` scan absurd would need the sample first.
- **No multi-column or expression statistics.** `WHERE lower(name) = 'ada'`
  has nothing to read, and neither does a correlated pair of columns.
- **The histogram cannot resolve a single value inside a bucket.** `<` and `<=`
  differ by the equality estimate added on top, which is PostgreSQL's answer
  too, and it slightly double-counts.
