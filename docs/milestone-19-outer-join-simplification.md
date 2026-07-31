# Milestone 19: Proving an outer join is an inner join

Milestone 18 ran an outer join exactly where it was written and never asked
whether it had to be one. Often it does not:

```sql
SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id
WHERE o.total > 200;
```

The `LEFT` keeps every user who placed no order and fills `o`'s columns with
NULLs. Then `NULL > 200` is NULL, a `WHERE` keeps only TRUE, and not one of those
rows reaches the output. Preserving them was work with no observer. This is an
inner join written the long way, and this milestone is the planner noticing.

The rewrite itself saves nothing. An inner join and an outer join over the same
inputs cost the same to run. What it saves is everything Milestone 18 had to
forbid, and it had to forbid a lot.

---

## What was built

| File | What changed |
|---|---|
| `engine/optimizer/nullability.py` | new. A three-valued abstract interpreter, and `rejects_nulls` |
| `engine/optimizer/rules.py` | `_simplify_outer_joins`, the fifth rule, and a module docstring that had claimed there were no joins |
| `engine/parser/ast.py` | `JoinKind.of(preserve_left=, preserve_right=)`, the inverse of the two properties Milestone 18 added |
| `tests/unit/test_null_rejection.py` | new. The soundness property, exhaustively |
| `engine/planner/physical.py` | `_joined_distinct_counts`: the estimate this milestone's own measurement found broken |
| `tests/unit/test_joins.py` | 30 more cases, nine of them about when the rule must **not** fire |
| `tests/differential/generator.py` | the anti-join idiom drawn on purpose, and two coverage labels |

---

## The analysis

Every serious planner has this and every one states it the same way. A predicate
**rejects NULLs** for a set of tables when it cannot evaluate to TRUE with every
column of those tables set to NULL. PostgreSQL calls the walk
`find_nonnullable_rels`; the literature calls the property null-rejecting, or
null-intolerant, after Galindo-Legaria and Rosenthal.

The obvious implementation asks whether each operator is *strict*: does it return
NULL when any argument is NULL? That is the right shape for PostgreSQL, whose
operators are catalogue rows with a strictness flag on them. ChenDB has a fixed
handful and no function catalogue, so the truth tables can simply be written out,
and writing them out buys two things a strictness flag does not:

* **`IS NULL` is not strict.** It is the one predicate that goes *TRUE* about a
  row the join invented, which is exactly the anti-join idiom, and exactly what
  must never be rewritten.
* **`OR` is not strict either.** `NULL OR TRUE` is TRUE, so an `OR` rejects only
  when both sides do.

### Sets of outcomes, not outcomes

Evaluating a predicate needs a row. The question here has no row, so the analysis
evaluates over **sets of possible results** instead. Every column of a
NULL-supplied table is known to be NULL. Everything else is unknown and could be
anything. Push those two facts through SQL's own truth tables:

```
  rejects  sh.hours > 5                       could be {UNKNOWN}
    ...    sh.hours IS NULL                   could be {TRUE}
  rejects  sh.hours IS NOT NULL               could be {FALSE}
  rejects  sh.hours > 5 AND s.team = 'core'   could be {FALSE, UNKNOWN}
    ...    sh.hours > 5 OR s.team = 'core'    could be {TRUE, UNKNOWN}
  rejects  NOT (sh.hours = 5)                 could be {UNKNOWN}
    ...    s.team = 'core'                    could be {FALSE, TRUE, UNKNOWN}
```

TRUE absent from the set is a proof that TRUE is impossible.

This is abstract interpretation, and it is sound because every rule returns a
**superset** of what can really happen. The direction matters and it is the one
design decision in the module: an unrecognised expression returns
`{TRUE, FALSE, UNKNOWN}`, so a new AST node costs a missed rewrite rather than a
wrong answer. Defaulting the other way would mean every future node silently
licences a rewrite until somebody notices.

---

## The rewrite is one line, applied twice

A join is not four names. It is two independent booleans, which Milestone 18
already knew (`preserves_left`, `preserves_right`) but only in one direction.
Naming the inverse, `JoinKind.of`, is what turns five special cases into one
rule:

```
LEFT  ─(the right side is null-rejected)─▶  INNER
RIGHT ─(the left side is null-rejected)──▶  INNER
FULL  ─(the right side is null-rejected)─▶  RIGHT
FULL  ─(the left side is null-rejected)──▶  LEFT
FULL  ─(both)────────────────────────────▶  INNER
```

`FULL` is why this is worth saying. It is the only kind that can be reduced and
still be outer, and a rewrite written as a table of name-to-name cases would have
needed two entries nobody thinks to add. Written as two booleans it needs none.

Note which side is which. `preserves_left` is what *creates* NULLs on the
**right**, because the rows it keeps are the left rows that found no partner. The
two lines read backwards from the naive expectation and that is not a typo.

### What counts as evidence

The WHERE, obviously. And also the `ON` of any **later** join in the chain that
does not preserve its left input, because such a join discards an accumulated row
its `ON` rejects. An inner join is one. So is a `RIGHT` join: an `X RIGHT JOIN Y`
drops an `X` row that matches no `Y`. That makes this an inner join with no WHERE
at all:

```sql
SELECT u.name FROM users u
  LEFT JOIN orders o ON u.id = o.user_id
       JOIN tags   t ON o.id = t.order_id;
```

`o.id = t.order_id` is NULL for every row the `LEFT` invented, and the inner join
above drops it. Swap that `JOIN` for a `LEFT JOIN` and the evidence disappears,
because a preserving join keeps those rows instead of rejecting them.

Three things are deliberately **not** evidence:

* **The outer join's own `ON`.** `a LEFT JOIN b ON b.x = 5` cannot be TRUE about
  an invented row either, and it is still not a reason to rewrite: an `ON`
  decides which rows *match*, and the rows that do not match are precisely the
  ones the join preserves. Treating an `ON` like a `WHERE` is the entire
  difference between the two clauses, and it is the bug this rule is one step
  away from at all times.
* **An earlier join's `ON`.** It ran before the NULLs existed, so it never saw
  them.
* **`HAVING`.** It runs after grouping, and what a NULL does to a group is a
  longer argument than this rule needs to make. Declining costs a rewrite, not an
  answer.

The chain is walked **outermost join inward**, so a join already reduced this pass
counts as its reduced kind for the ones inside it. A `FULL` that became a `RIGHT`
starts discarding accumulated rows, and its `ON` becomes admissible evidence for
the join beneath it. One pass, not to a fixed point: enough for that, and not
enough for every chain a person could construct.

---

## What it unblocks

This is the whole return on the milestone, and none of it is in the rule.

### Predicate pushdown, in the direction that was closed

Milestone 18 protects a NULL-supplied table from pushdown, correctly:

> pushed to `b`'s scan and consumed, the surviving `a` rows come back
> NULL-extended, when the WHERE should have rejected them

Once the join is inner there is nothing to protect, and the predicate that proved
the rewrite is the one that gets pushed. It filters `orders` before the join
rather than filtering the join's output, and it can reach an index doing it:

```
before                                     after
─────────────────────────────────────      ─────────────────────────────────────
PhysicalFilter  (total = 42)               PhysicalHashJoin  user_id = id
  └─ PhysicalHashJoin  LEFT id = user_id     └─ PhysicalIndexScan  total = 42
    └─ PhysicalSeqScan  table=users          └─ PhysicalSeqScan  table=users
    └─ PhysicalSeqScan  table=orders
```

Measured on 50 users and 4,000 orders, median of 25 runs:

| | before | after | |
|---|---|---|---|
| no index on `orders.total` | 19.4 ms | 13.4 ms | 1.5x |
| index on `orders.total` | 19.4 ms | 0.47 ms | **41x** |

The 41x is the honest headline and also the honest caveat: it is the index doing
the work, and the rewrite is what let the query reach it. Without one the win is
the 1.5x, which is a scan that stops carrying 4,000 rows into a hash probe.

### The estimate, which turned out to be broken

Measuring the above is what found it. The plan got faster and its *estimated
cost went up*, which should not happen, and the reason was not in this milestone
at all:

```
  JOIN        estimated     50.0   actual 4000
  LEFT JOIN   estimated     50.0   actual 4000
  RIGHT JOIN  estimated   4000.0   actual 4000     <- the floor, by luck
  FULL JOIN   estimated   4000.0   actual 4000     <- the floor, by luck
```

`join_selectivity` computes `1 / max(distinct(a.x), distinct(b.y))`, which is the
textbook estimate and the right one. It was passing **row counts** where distinct
counts belong. For 50 users joined to 4,000 orders that is `50 * 4000 / 4000`,
which is 50: the size of the wrong side, off by 80x, and compounding to 6,400x on
three tables.

`distinct_join_selectivity` has spelled the correct formula since Milestone 6 and
nothing had ever called it. Wiring it up is four lines, and it needed no test to
change, which is its own small comment on how long an unused function can sit
next to the used one.

Two things kept it hidden. Milestone 18's floor rescued `RIGHT` and `FULL` by
accident, so the bug was only visible in half the cases. And an estimate that is
too *low* does not produce a visibly stupid plan on small fixtures; it produces a
slightly wrong join order that still finishes.

It belongs in this milestone rather than the backlog because this milestone is
what makes it matter. A rewritten join rejoins the order search, and a search
cannot choose between plans it cannot size.

### Join reordering, which an outer join is a barrier to

`_plan_chain` treats an outer join as a barrier the System R search may not cross,
and `EXPLAIN` says so in as many words. Two `LEFT` joins the search may not touch
become two inner joins it may order however it likes:

```
  (no WHERE)               LEFT then LEFT     search is barred
  WHERE n.body = 'late'    INNER then INNER   search is free
```

Both wins arrive through parts of the planner this rule never mentions, which is
the argument for putting it in `rules.py` at all rather than inline anywhere.

---

## Verification

The failure mode here is not a crash. A rule that fires too eagerly returns
**fewer rows, quietly**, and nothing downstream would attribute the loss to the
optimiser. So the testing is weighted at that, and more than half the new cases
are about when the rule must not fire.

**An exhaustive soundness property.** `test_null_rejection.py` builds every
predicate in a fixed list, and for each one the analysis claims to reject, runs
it through the engine's real evaluator against every assignment of values to the
preserved side. If any of them comes out TRUE, the rule would delete that row.
The stronger version of the same test asserts the abstract answer *contains* the
real one, which is the property soundness actually rests on.

The truth tables are checked against `evaluate` rather than against a second
opinion written in the test file, because a second copy of three-valued logic is
a thing that can drift.

**The rewrite is compared against itself.** `apply_rules` reads the module-level
`RULES` at call time, so removing one entry plans the same SQL both ways. Eight
queries, some the rule fires on and some it declines, and every answer has to be
identical. That is the contract of a rewrite rule, checked rather than asserted.

**320,000 generated query pairs agree with SQLite.** The differential suite
already produced this shape in quantity: outer joins are about 40% of generated
joins and 84 of the 1,024 CI-seed queries put a WHERE on a NULL-supplied side.
What it produced by accident was the anti-join idiom, five times across 64 seeds,
which is a floor rather than coverage. It is now drawn on purpose, 35 times, and
both shapes have names in `REQUIRED_FEATURES` so that losing either fails the
build instead of leaving it green.

**Every guard was watched failing.** Removing the rule from `RULES` turns 17 of
the new join tests red. Making `OR` behave like `AND` in the analysis, the most
plausible single wrong turn, turns 13 null-rejection tests red including the
exhaustive property.

One existing test had to change, and it is worth naming rather than burying:
`test_a_predicate_on_the_null_supplied_side_is_not_pushed_down` used
`WHERE o.total > 200`, which this milestone turns into an inner join and pushes
down, correctly. The invariant it guards is real, so it now uses
`WHERE o.total > 200 OR o.total IS NULL`, which can be TRUE about an invented row
and therefore still has an outer join to protect.

---

## Try it

```bash
.venv/bin/python examples/milestone19_outer_join_simplification.py
.venv/bin/python -m pytest -q tests/unit/test_null_rejection.py tests/unit/test_joins.py
.venv/bin/python scripts/differential.py --seeds 0:20000
```

---

## What is still missing

- **No reordering across an outer join that survives.** This milestone removes
  the barrier when it can prove the join away. It does not make the barrier
  permeable. PostgreSQL's `min_lefthand`/`min_righthand` per `SpecialJoinInfo` is
  how that is recovered, so the search can prove a reordering safe by set
  containment rather than assume it is not, and it is a milestone of its own.
- **No join-to-semijoin rewrite.** `WHERE EXISTS (…)` is the shape that wants it
  and there are no subqueries yet.
- **One pass, not a fixed point.** Outermost first recovers most of what a fixed
  point would, and the rest needs a termination argument nobody has needed yet.
- **`HAVING` is not consulted**, as above.
- **The analysis knows only the operators the engine has.** No `CASE`, no
  `COALESCE`, no functions, because there are none. Each returns "could be
  anything", which declines the rewrite rather than guessing at it.
- **The cardinality estimate is still crude.** It is no longer wrong by 80x, but
  `distinct_count` is a single number and skew defeats it: ten million orders
  spread over three customers is still `distinct = 3`. A most-common-values list
  is what fixes that, and it is the largest thing still missing from the cost
  model.
