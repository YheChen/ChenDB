# Milestone 13: Joins, aggregation, and a planner with a job

Twelve milestones of `SELECT` meant *one table, filtered, projected*. Four
operators, and a planner whose only decision was which of two ways to read a
single heap.

That planner has been carrying a cost model calibrated by measurement since
Milestone 6, an alternatives panel that shows what it rejected, and a
`geqo_threshold` reference in a docstring, for a search space of size two.
This milestone gives it something to do:

```
  SELECT c.city, COUNT(*) AS orders, SUM(s.amount) AS revenue
  FROM customers c JOIN sales s ON c.id = s.customer_id
  WHERE s.amount > 20
  GROUP BY c.city HAVING SUM(s.amount) > 100
  ORDER BY revenue DESC LIMIT 5
```

Two algorithms to choose between, an order to join in, a predicate to place, and
a pipeline that stops twice.

---

## What was built

```
engine/parser/ast.py         JoinClause, FunctionCall, OrderByItem, aliases
engine/parser/parser.py      JOIN … ON, comma joins, GROUP BY, HAVING,
                             ORDER BY, LIMIT/OFFSET, aggregate calls
engine/executor/binder.py    Scope, RowLayout, aggregate splitting, sort keys
engine/planner/logical.py    LogicalJoin, LogicalAggregate, LogicalSort, LogicalLimit
engine/planner/physical.py   join-order search, pushdown, four physical nodes
engine/optimizer/cost.py     five new constants, all measured
engine/executor/operators.py NestedLoopJoin, HashJoin, HashAggregate, Sort, Limit
```

Grammar:

```
select    := SELECT select_list FROM from_clause [ WHERE expr ]
             [ GROUP BY expr {,…} ] [ HAVING expr ]
             [ ORDER BY sort_item {,…} ] [ LIMIT int [ OFFSET int ] ]
from      := table_ref { ',' table_ref | [INNER] JOIN table_ref ON expr }
table_ref := ident [ [ AS ] ident ]
aggregate := ( COUNT | SUM | AVG | MIN | MAX ) '(' ( '*' | expr ) ')'
```

Aggregate names are **not** reserved words. They are parsed as an identifier
followed by `(`, which is what PostgreSQL and SQLite do and which keeps `count`
usable as a column name. A table with a `min` and a `max` column is not an
unusual table.

---

## The decision that made the row layout easy

An inner join is commutative, so the planner reorders freely (an outer one is not: Milestone 18). But a bound column
index (`c.city` is index 2, `s.amount` is index 5) was computed by the binder
against the order the tables were *written* in. If the physical row's shape
followed the *join* order, every index would have to be remapped at every level.

So it doesn't:

```
  FROM customers c JOIN sales s

    [ c.id  c.name  c.city │ s.id  s.customer_id  s.amount ]
       0      1       2       3         4             5
```

**A row's layout is the written order, always.** Below the topmost join every
row is that full width, with the tables not yet joined left empty, and a scan
places its own columns into its own slice. A join copies the right side's slices
into the left side's row.

By slice, and not by "take whichever side isn't `None`". That shortcut is one
line shorter and wrong, because a genuine SQL NULL is indistinguishable from an
empty slot and would be silently overwritten.

The cost is width. A real engine projects away what it no longer needs as early
as it can, and pays for that with a bound-index-to-physical-position mapping
threaded through the whole executor. This trades the width for never needing the
mapping, and the trade is why join reordering needed no changes above the join
at all.

---

## Two algorithms, and why one nearly always wins

| | |
|---|---|
| **Nested loop** | For every left row, every right row. Works on *any* predicate. |
| **Hash join** | Build a table on one side, probe with the other. Needs an equality. |

Priced by the measured constants, on 40 customers and 800 sales:

| | cost |
|---|---|
| nested loop | 4,480 |
| hash join | 21.5 |
| | **209×** |

So the hash join wins every equijoin the cost model has been shown, and the
nested loop is kept for the case where there is nothing to hash:

```
SELECT c.id FROM customers c JOIN sales s ON c.id < s.amount

PhysicalNestedLoopJoin  (id < amount)  (cost=4850.2 rows=10667)
```

That is why a range join is slow in every engine and not just this one, and the
plan says so rather than quietly being quadratic.

### The build side is arithmetic, not a rule

Memory is proportional to the build side, so the small side should be built. The
cost model expresses that by pricing the two operations separately:

| | measured | unit |
|---|---|---|
| hash-table insert | 67 ns | 0.037 |
| hash-table lookup | 45 ns | 0.025 |

A build costs half again what a probe does, so putting the bigger side on the
build is simply more expensive and the planner picks the small one without
being told to. Rules that cannot be outvoted by evidence are how cost models rot.

---

## Choosing an order

System R's dynamic programme, over left-deep trees. Solve every one-table set,
build every two-table set from those, and so on. The best plan for `{a,b,c}`
uses the best plan for one of its subsets, so each subset is solved once instead
of re-derived down every branch that contains it.

| tables | left-deep orders | DP subsets |
|---|---|---|
| 3 | 6 | 27 |
| 5 | 120 | 243 |
| 8 | 40,320 | 6,561 |
| 12 | 479,001,600 | 531,441 |

`MAX_TABLES_TO_ENUMERATE` is 8; above it the planner joins the cheapest visible
pair repeatedly and **says so in the plan**, because a planner that silently
degrades is worse than one that admits it. PostgreSQL's threshold is 12 and it
switches to a genetic algorithm; the number here is lower because this DP is
written for clarity rather than speed.

**Left-deep only.** Joining `(a⨝b)` to `(c⨝d)` is sometimes better and
multiplies the search space again. System R excluded bushy plans in 1979 for
that reason and most optimizers still do.

### `ON` and `WHERE` go into the same pool

> Milestone 18 note: this holds for an *inner* join only, which the section
> below is careful to say. An outer join's `ON` is kept out of the pool
> entirely, see `docs/milestone-18-outer-joins.md`.

For an inner join they mean the same thing: `a JOIN b ON p` and `a, b WHERE p`
produce identical rows. So every conjunct from every `ON` and from the `WHERE`
goes into one list, and each is placed wherever it belongs, which is how a
condition written as an `ON` can end up pushed down to a scan, and one written
in the `WHERE` can become the join key.

---

## Pushdown is a rewrite, not an alternative

```
PhysicalHashJoin  id = customer_id
  PhysicalFilter  (city = 'london')      ← below the join
    PhysicalSeqScan  table=customers
  PhysicalSeqScan  table=sales
```

A conjunct naming one table is applied at that table's scan, where it shrinks
the input to every join above it. Pushing it down can never be worse, which is
exactly what makes it a **rewrite** and not a costed candidate. The textbook
distinction between the two, made concrete.

It is also what makes an index reachable at all. A predicate left above a join
is applied to join output, where no index exists; pushed to the scan it becomes
an access-path decision again:

```
Considered how to read customers:
     Sequential scan of customers  cost=18.2 rows=40  [1.9x the cost of the chosen plan]
  -> Index scan on customers_city (city = 'london')  cost=9.6 rows=10
```

---

## Aggregation, and the two places the pipeline stops

Everything before this milestone streamed: a row entered at the scan and left at
the top without anything holding on to it. `HashAggregate` and `Sort` both read
their entire input before producing a single row.

That shows up as the point where *time to first row* stops being small, and it
is the honest reason `LIMIT 3` over a `GROUP BY … ORDER BY` saves nothing at
all, the plan shows the child's cost not falling.

### The grouped row

```
  [ key₀ … keyₖ₋₁ , agg₀ … aggₘ₋₁ ]
```

Every projection is rewritten by the binder to index into *that* row rather than
the joined one, and rewritten to a plain `BoundColumnRef`, because that node
already means "the value at index *i* of the row I was handed". The expression
evaluator needed no change at all.

A column that is neither a grouping key nor inside an aggregate is an error:

```
SELECT name, city, COUNT(*) FROM users GROUP BY city
ERROR: 'u.name' must appear in GROUP BY or be used in an aggregate;
       a group is many rows and they do not agree on it
```

MySQL historically picked one of the values and called it a feature.

### Three NULL rules that are each a different question

| | |
|---|---|
| `COUNT(*)` vs `COUNT(x)` | rows against non-NULL values. Not the same question, and the AST keeps them apart, `argument` is `None` for the star form. |
| `SUM`/`AVG` ignore NULLs | `AVG([1, NULL, 3])` is 2, not 1.33. A NULL is a row that does not participate, not a zero. |
| over no rows | `COUNT` is 0; everything else is NULL. Calling `SUM` zero would make an empty table and a table of zeros indistinguishable. |

And one more that is easy to get backwards: with no `GROUP BY` there is exactly
**one** group and it exists even over no rows, so `SELECT COUNT(*) FROM empty`
is `0`. Add a `GROUP BY` and the same query over the same table returns nothing
at all, because there are as many groups as there are values and there are none.

### Sorting

NULLs last ascending, first descending, PostgreSQL's default, the opposite of
SQLite's. The standard leaves it implementation-defined, so there is no right
answer; there is a wrong one, which is comparing NULL to a number and crashing.

The implementation is a partition digit in front of the sort key:

```python
def _sort_key(value):
    return (1, 0) if value is None else (0, value)
```

`reverse=True` flips the partition along with the values, so descending puts
NULLs first by *not* having a special case rather than by having one.

Several keys are applied least-significant-first in separate passes. Python's
sort is stable, so the earlier keys survive, shorter and less error-prone than
one comparator mixing ascending and descending.

---

## What EXPLAIN had to learn

With one table there was one decision, and a flat list of alternatives was
right. With joins there are several independent ones, and a flat list reads as a
contradiction, three entries all marked "chosen":

```
Decided how to read customers: Sequential scan of customers
Decided how to read sales: Sequential scan of sales
Considered what order to join in:
  -> customers x sales x lines  cost=0.2 rows=1
     lines x customers  cost=0.8 rows=6  [a building block, not a rejected plan]
     customers x sales  cost=0.2 rows=2  [a building block, not a rejected plan]
```

So every `Alternative` now carries the *question* it answered, and both `EXPLAIN`
and the visualizer's panel group by it. The two-table sub-plans are reported
too, because that is where "join the small side first" is visible, labelled as
building blocks rather than rejected plans, which they are not.

---

## The Milestone 12 guard caught something on its first outing

`ORDER BY` became implemented, and the SQL editor's example labelled *"Not
implemented yet"* (which was `SELECT * FROM users ORDER BY age`) started
working:

```
demo SQL does not match what demoSql.ts claims:
  editor/Not implemented yet (Not implemented yet) was accepted
```

That is precisely the failure the guard was built for, one milestone after it
was built, and it took four seconds to diagnose instead of shipping. The example
is now `LEFT JOIN`, which is genuinely still refused.

---

## What it costs

Measured on the data the example builds, 40 customers, 800 sales, 1,600 lines.

| | |
|---|---|
| `COUNT(*)` over 800 rows | 2,408 µs |
| the same, grouped 40 ways | 2,621 µs |

Grouping is nearly free once the rows are read, which is the property that makes
hashing the right choice: linear in rows and independent of the group count.
Sorting first would cost `n log n` and buy an ordering nobody asked for.

The five new constants, all measured in the project's existing unit (one page
miss = 1,822 ns):

| | ns | units |
|---|---|---|
| hash insert | 67 | 0.037 |
| hash lookup | 45 | 0.025 |
| tuple compare | 26 | 0.014 |
| accumulate | 50 | 0.027 |

A tuple comparison is a fifth of a predicate evaluation (it is a `memcmp` in C
rather than a walk of an expression tree) which is why an `n log n` sort of
5,000 rows costs less than the scan that produced them.

---

## Try it

```bash
python examples/milestone13_joins.py
```

```bash
python -m pytest tests/unit/test_joins.py -v
```

In the explorer, the SQL editor's **Joins and aggregation** example builds its
own two tables and runs the query at the top of this document. Read the plan
beside it.

---

## What is still missing

- **Outer joins.** `LEFT`/`RIGHT`/`FULL` are refused by name. An outer join
  fixes which side may be the outer relation, so the planner could no longer
  reorder freely, and the executor would have to NULL-extend. It is a milestone,
  not a flag.
- **Index nested-loop join.** With an index on the inner side and a tiny outer
  side, probing the index per outer row beats building a hash table. ChenDB
  never considers it, so a join key that is indexed buys nothing.
- **Bushy plans**, as above.
- **Skew.** Join cardinality is `1 / max(distinct_left, distinct_right)`, which
  assumes every value is equally common. Ten million orders spread over three
  customers is still `distinct = 3` and the estimate is off by six orders of
  magnitude. A most-common-values list is the single biggest thing missing from
  the cost model.
- **Spilling.** Hash tables and sorts are memory-only. PostgreSQL switches to an
  external merge at `work_mem`; a sort here that does not fit is a sort that
  fails, bounded only by the row ceiling.
- **`DISTINCT`, subqueries, `UNION`, window functions, `COUNT(DISTINCT x)`.**
- **`ORDER BY` over an expression not in the select list.** Sorting happens
  above the projection, so a sort key has to be something the projection
  produced, by ordinal, by output name, or by repeating the expression.
  PostgreSQL sorts below the projection with a wider intermediate row.
