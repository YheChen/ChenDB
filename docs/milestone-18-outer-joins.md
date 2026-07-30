# Milestone 18 — Outer joins, and the licence the planner had to give up

`LEFT`, `RIGHT` and `FULL OUTER JOIN`. The parser had refused them by name for
five milestones, with a message that turned out to be exactly right about why:

> an outer join constrains the order the planner may join in, and ChenDB reorders
> freely

That is the milestone. NULL-extending an unmatched row is nearly free — a
consequence of a decision made in Milestone 13 — and the entire cost is in the
planner, which had been built on a licence it no longer has.

---

## What was built

| File | What changed |
|---|---|
| `engine/parser/parser.py` | `_join_kind` accepts the three sides; `OUTER` is noise |
| `engine/parser/ast.py` | `JoinKind.is_outer`, `.preserves_left`, `.preserves_right` |
| `engine/executor/binder.py` | `BoundJoin.kind` — the constraint has to travel |
| `engine/planner/logical.py` | `LogicalJoin.kind` |
| `engine/planner/physical.py` | `PhysicalJoin` base with `preserve_left`/`preserve_right`; `_join_steps`, `_null_supplied`, `_plan_chain`, `_plan_segment`; `_join_cardinality` reads terms, not pool positions |
| `engine/executor/operators.py` | NULL extension in both algorithms |

---

## Why the executor was the easy half

An unmatched left row does not need to be *extended*. It is already extended.

Milestone 13 decided that **a row's layout is the written order of the `FROM`,
always** — every row below the topmost join is the full width of the query, with
the tables not yet joined left as `None`. It paid for that in row width, and
documented the cost. This is the refund:

```
  users ⟕ orders,  users row with no partner

  [ 1, 'ada', 'london', None, None, None ]
    └── users ──────┘  └── orders ────┘
                        never written
```

`_merge` copies the right side's slices into the left row. A left row that found
no partner has simply never had that copy done to it, so emitting it unchanged
*is* the NULL extension. The mirror holds for an unmatched right row: its subplan
never touched the left side's slots.

So the operator work is bookkeeping, not construction — and the bookkeeping is
where the one real bug was.

### The bug the fuzzer found and nine hand-written cases did not

`HashJoin` emitted an unmatched probe row when its bucket was *empty*. That is
the obvious condition and it is not the right one:

```sql
FULL JOIN child ON parent.pid = child.parent_id AND child.parent_id > 0
```

A hash join hashes the equality and re-checks the rest per pair, so a probe row
can hash to a full bucket and be rejected by the residual for every candidate in
it. Its bucket was not empty; it matched nothing. Two of three rows went missing.

I had tested `FULL JOIN` by hand, nine ways, and every one of my cases had a bare
equality for its `ON`. The generative suite from Milestone 17 found this on its
first run with outer joins enabled — because 35% of the outer joins it emits carry
an extra term on the null-supplied side, which is a corner I put in the generator
*because* it is the shape the planner had to get right, and it caught the executor
instead.

The fix is a `_probe_matched` flag. "Is the bucket empty" and "did this row match"
are different questions, and only the second one is the definition of unmatched.

### And what NULL keys mean now

`HashJoin` used to drop a NULL-keyed row from both sides, with a comment that this
is not an optimisation but what `=` means in three-valued logic. Both halves of
that are still true, and the conclusion has changed: a row that *cannot* match is
the definition of unmatched, so for a preserved side it must be **emitted**. It
stays out of the hash table and stays in the build list.

---

## The planner was the whole milestone

### `ON` and `WHERE` are no longer the same pool

Milestone 13's `plan_select` put the `WHERE` and every join's `ON` into one flat
list of conjuncts, and its comment said why:

> For an inner join the two are interchangeable — `a JOIN b ON p` and
> `a, b WHERE p` mean the same thing

Exactly so, and every word of it fails for an outer join:

```sql
SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id AND o.total > 200;
--> every user survives; alan, whose only order is 80, comes back NULL-extended

SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE o.total > 200;
--> alan is gone: NULL > 200 is NULL, not TRUE, so the WHERE rejects him
```

Both are correct and they are different queries. `EXPLAIN` shows the difference
directly:

```
-- inner: the condition is pushed BELOW the join
PhysicalHashJoin  id = aid
  └─ PhysicalSeqScan  table=a
  └─ PhysicalFilter  (y > 100)
    └─ PhysicalSeqScan  table=b

-- outer: it stays AT the join
PhysicalHashJoin  LEFT id = aid AND (y > 100)
  └─ PhysicalSeqScan  table=a
  └─ PhysicalSeqScan  table=b

-- outer, condition in the WHERE: it stays ABOVE the join
PhysicalFilter  (y > 100)
  └─ PhysicalHashJoin  LEFT id = aid
```

So an outer join's `ON` never enters the pool. It is not pushed down, not pulled
up, and not merged with anything.

### Pushdown becomes conditional, in one direction only

Predicate pushdown was described in Milestone 13 as a *rewrite* rather than a
costed alternative, because it can never be worse. It still cannot — except into
a table an outer join above can NULL-extend:

```sql
SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE o.total > 200;
```

Push `o.total > 200` to `orders`' scan and consume it, and the users whose orders
were all small come back NULL-extended, when the `WHERE` should have removed
them. `_null_supplied` computes the set of positions this applies to, and those
tables get no pushdown.

Pushing into the *preserved* side is still legal and still happens, which is what
keeps `... LEFT JOIN ... WHERE u.city = 'london'` fast. `RIGHT` is why the set has
to be computed by walking the chain rather than read off a single step: it
null-extends everything accumulated to its left.

### An outer join is a barrier in the search

The System-R dynamic programme enumerates every left-deep order over a set of
relations. Its licence to do that is that an inner join is commutative and
associative. An outer join is neither.

`_plan_chain` walks the written chain left to right. Consecutive inner joins
accumulate into a **segment** the search may order however it likes. An outer join
closes the segment, runs at that point with its own `ON`, and the result becomes
**one opaque relation** that the next segment's search sees as a single input.

Opaque is exactly the right amount of freedom, and it falls out rather than being
enforced:

* the search **can commute** it with other relations — an inner join's two inputs
  may be swapped, so joining `c` to `(a ⟕ b)` either way round is sound;
* it **cannot re-associate** into it, because the relation is one item in the
  search's world and there is nothing inside to reach. It can never build
  `a ⟕ (b ⨝ c)` from `(a ⟕ b) ⨝ c`, and those are different queries.

Build-side selection survives intact, and that is worth spelling out because it
looks like it should not. `preserve_left` and `preserve_right` describe the
**physical** inputs, not the `LEFT` or `RIGHT` in the query. Swapping an outer
join's two inputs and flipping the flags produces the identical output row —
because `RowLayout` fixes every column's position by written order — so the cost
model keeps its freedom to hash the smaller side. Only the order *relative to
other joins* is constrained.

**What this gives up**, stated rather than hidden: an inner join written after an
outer one cannot move before it, even where that would be legal and cheaper.

The general treatment is PostgreSQL's. It builds a `SpecialJoinInfo` per outer
join carrying `min_lefthand` and `min_righthand` relation sets, so the search can
*prove* a particular reordering safe by set containment instead of assuming it is
not — and it has identity-3 and identity-2 rules for the specific cases where an
outer join and an inner join do commute. That is the right answer for a planner
that must be fast on twelve-table queries. Here it would be a substantial amount
of machinery to recover orderings for a shape — an outer join with inner joins
after it — whose cardinality the cost model cannot estimate well anyway. The
honest version is one line in `EXPLAIN`:

```
Decided what order to join in: a LEFT b — an outer join runs where it was
written, so the search may not reorder across it
```

### The cost model had a floor, and then a worse problem

An outer join has a **floor** an inner join does not: every preserved row appears
whether it matched or not, so an estimate below the preserved side's own row count
is not merely imprecise but impossible. PostgreSQL clamps the same way in
`calc_joinrel_size_estimate`.

Adding the clamp exposed something larger. `_join_cardinality` took *positions
into the shared conjunct pool* — and an outer join contributes nothing to that
pool, so it received an empty list and returned the raw product. Every outer join
was being costed as a **cross product**: 60 rows joined to 300 estimated at
18,000 instead of 90, with the error compounding into every operator above it.

It now takes the terms themselves, whatever they came from. That is the better
interface regardless: an estimator should read the conditions it is estimating,
not an index into a list that happens to contain them.

---

## What it costs

Measured on the fixture in `examples/milestone18_outer_joins.py`.

| | |
|---|---|
| Hash join, inner | baseline |
| Hash join, preserve the probe side | one flag test per probe row |
| Hash join, preserve the build side | one `set` insert per matched pair, one pass over the build list at the end |
| Nested loop, either side | one flag per left row, one `set` of matched right indices |

The build-side cost is the interesting one: the buckets now hold **indices** into
a list of build rows rather than the rows themselves, so a preserved build side
can find the leftovers. The rows are shared, so that is one integer per build row
over the previous layout — and it is what makes `LEFT JOIN` work when the planner
chose to hash the preserved side.

The matched set is `set[int]`, not `set[Row]`, and that is not a micro-decision:
two identical build rows are two rows, and a set of row *values* would treat them
as one, so an unmatched duplicate would go missing.

---

## Verification

The claim worth making is not that the tests pass. It is that **160,000 generated
query pairs agree with SQLite**, with outer joins in about 40% of the generated
joins and an extra `ON` term on the null-supplied side in a third of those.

Milestone 17's generator needed one change to cover this — `_join_clause` picks a
kind — and the schema needed none. It already drew `child.parent_id` from keys the
parent has, keys it has not, and NULL, which was written for exactly this:

> That is the difference between exercising a join and merely calling one, and it
> is what Milestone 18's outer joins will need.

Two guards fired as designed while this landed:

* `test_demo_sql.py::test_every_demo_statement_parses` failed with
  `editor/Not implemented yet (Not implemented yet) was accepted`. That slot has
  now had three occupants — `ORDER BY` until Milestone 13, `LEFT JOIN` until this
  one, `DISTINCT` now — and it has caught its own staleness every single time. An
  example of what an engine cannot do is a claim with a shelf life.
* `examples/milestone2_parser.py` asserts that a list of statements produce
  errors, and `LEFT JOIN` was in it. `make examples` runs on a bare interpreter in
  CI, so that failed too — the second time that same line has needed updating.

---

## Try it

```bash
.venv/bin/python examples/milestone18_outer_joins.py
.venv/bin/python -m pytest -q tests/unit/test_joins.py
.venv/bin/python scripts/differential.py --seeds 0:5000
```

---

## What is still missing

- **No reordering across an outer join**, as above. PostgreSQL's
  `min_lefthand`/`min_righthand` is the way to recover it, and it is a milestone
  of its own.
- **No outer-join simplification.** `a LEFT JOIN b ON … WHERE b.x = 5` is
  provably an inner join, because a null-rejecting `WHERE` on the null-supplied
  side discards every row the outer join preserved. Every serious planner spots
  that and rewrites it, which both removes the barrier and re-enables pushdown.
  ChenDB executes the outer join and then filters — correct, and slower than it
  needs to be for a query people write often.
- **No `CROSS JOIN` keyword**, still. `FROM a, b` is the same thing and the parser
  says so.
- **No `USING` and no `NATURAL JOIN`.** Both are sugar over `ON`, and both need
  the binder to merge two columns into one output column — which the flat
  `RowLayout` has no way to express, since it maps every input column to a
  distinct position.
- **`FULL JOIN` always materialises the build side.** So does the nested loop.
  Neither can stream, which is true of `LEFT` on the build side too — a preserved
  side cannot be emitted until its input is known to be exhausted.
- **The cardinality estimate is still crude.** The floor is right and the cross
  product is gone, but `join_selectivity` has no notion of a foreign key, so a
  join along one is estimated the same way as a join between unrelated columns.
