# Milestone 21: A third table, and the two bugs two could not find

This milestone was going to be reordering across an outer join. It is not,
because reading the code that would have to change turned up a query that
returns the wrong answer:

```sql
SELECT a.id, b.id, c.id FROM a
LEFT JOIN b ON a.id = b.ref
     JOIN c ON a.n  = c.ref;
```

Three rows. ChenDB returned none, and the plan it chose never read `c` at all.

A second one turned up ten minutes later, from the differential tester, as soon
as its generator could build three tables:

```sql
SELECT … FROM a JOIN b ON a.id = b.ref AND b.n > 100
RIGHT JOIN c ON b.id = c.ref;
```

Every row of `c` should come back NULL-extended. ChenDB dropped them.

Both shipped. Both survived 320,000 generated query pairs and 1,681 tests.
Neither is expressible with two tables.

---

## What was built

| File | What changed |
|---|---|
| `tests/differential/generator.py` | a `grandchild` table and a `chain` shape: three sources, two join clauses |
| `engine/planner/physical.py` | the two fixes, and predicates now carry which join they came from |
| `tests/differential/engines.py` | a fresh database per run, so the shrinker can re-run a case |
| `tests/differential/dialect.py` | the SQLite floor moved to 3.39, which is what `RIGHT JOIN` has needed since Milestone 18 |
| `tests/unit/test_joins.py` | four three-table tests, two of them regressions |

---

## Bug one: the join-order search planned two tables out of three

`_search_join_order` is System R's dynamic programme. It keyed its table of
best subplans by **which tables** a subplan covered:

```python
best = {relation.tables: relation for relation in relations}
everything = frozenset(range(len(relations)))
```

Those two lines are in different key spaces, and they agree only when every
input is a single table, because then the *n*th input covers table *n*. After an
outer join one input is a relation holding several tables at once, the spaces
diverge, and every `best.get(rest)` in the loop below missed.

The DP then found no subplan for any subset, left `best` holding only its
inputs, and returned whichever one happened to sit at `best[everything]`: the
outer join, alone. Every table joined after it vanished from the plan.

The residual predicate survived, so the query still compared `a.n` against the
column `c.ref` would have occupied. Nothing had written it, so it was NULL, so
the comparison was NULL, so **no rows**. A wrong answer, silently, and a plan
that a reader would have spotted immediately if anybody had printed one for this
shape.

The fix is one word: key by which *inputs*, not which tables.

## Bug two: an `ON` that could not be pushed down became a `WHERE`

Milestone 18 established, correctly, that a predicate must not be pushed into a
table an outer join can NULL-extend:

> pushed to `b`'s scan and consumed, the surviving `a` rows come back
> NULL-extended, when the WHERE should have rejected them

It applied that to **every** pooled conjunct. But the pool holds two different
things: the WHERE, which runs above every join, and every inner join's `ON`,
which runs at its own join and therefore *below* any outer join written after
it. For the second kind the protection is not merely unnecessary, it is
actively wrong.

Over-protected, the conjunct could be pushed nowhere. Unpushed, it went into the
residual. And a residual runs at the very top of the plan, which is exactly the
position that turns an `ON` into a `WHERE`. `b.n > 100` then rejected the rows
the `RIGHT` join existed to preserve, because their `b.n` was NULL.

The fix is that a conjunct now carries **where it came from**, and the
protection counts only the outer joins below that point:

```
WHERE           runs above every join   → protected from all of them
ON of join n    runs at join n          → protected from joins 0..n-1
```

One line in `plan_select` to record the origin, one parameter on
`_null_supplied`. The interesting part is not the code, it is that pooling the
WHERE with the inner `ON`s discards the one fact needed to protect them
differently, and Milestone 13's comment saying the two are interchangeable is
true only in the absence of an outer join.

---

## Why two tables could not find either

The generator built a parent and a child. That was enough for seven bugs in
Milestone 17 and for the `FULL JOIN` bug in Milestone 18, and it is structurally
incapable of producing either bug here:

- **Bug one** needs an outer join with a join *after* it, so that a
  multi-table relation and a single-table one meet in the search. Two tables
  give one join.
- **Bug two** needs an inner `ON` *below* an outer join, so three again.

So the milestone's real deliverable is the `grandchild` table and the `chain`
shape: `parent`, `child`, `grandchild`, two join clauses, each drawing its kind
independently, and the second clause linking to the child most of the time and
**over its head to the parent** the rest, which is the shape that gives a join
order something to prove.

Three new coverage labels are in `REQUIRED_FEATURES`, so losing any of them
fails the build rather than quietly narrowing the corpus:

| label | in 1,024 CI-seed queries |
|---|---|
| `three_table_join` | 130 |
| `join_skipping_a_table` | 40 |
| `inner_join_after_outer` | 33 |

## Two more things the third table shook out

**The shrinker could not shrink.** `chendb_outcomes` opens
`case<seed>.chendb` in the workspace it is handed, and the shrinker re-runs the
same case in the same workspace hundreds of times. The second run found the
file from the first and failed on `CREATE TABLE`, so every shrink report read
"the generated setup does not apply" instead of the divergence it was chasing.
It had presumably always been broken; nothing had diverged since the shrinker
was written.

**The SQLite version floor was a milestone out of date.** It said 3.30, which is
what `NULLS LAST` needs. Milestone 18 started generating `RIGHT` and `FULL OUTER
JOIN`, which SQLite has only supported since **3.39**, and did not move it. On
anything between, the guard would have passed and the campaign would have died
on a syntax error. Nobody noticed because every machine that ran it was newer.

---

## Verification

Each fix was watched failing against its own bug, and against the corpus:

| planted regression | hand-written | differential (CI seeds) |
|---|---|---|
| key the DP by tables again | 2 red | seeds 2 and 48 red |
| protect every conjunct alike | 1 red | seed 60 red |

That second column is the one that matters. The point of this milestone is not
that two bugs are fixed; it is that the corpus which could not see them now
sees them, on the sixty-four seeds CI runs on every push.

**320,000 generated query pairs agree with SQLite**, with three-table chains at
about 13% of them.

---

## What is still missing

- **Reordering across an outer join**, which is what this milestone set out to
  be and is now the next one. The machinery is PostgreSQL's `min_lefthand` and
  `min_righthand` per outer join, so the search can prove a reordering safe by
  set containment rather than assume it is not.
- **Four tables.** The chain is three, and the argument that three is enough is
  the same argument that said two was, which was wrong twice. A fourth would
  cost generator complexity and campaign time; the honest position is that the
  bound is unknown and three is where it currently sits.
- **The chain is a chain**, never a star: `grandchild` links to `child` or to
  `parent`, but two tables never both link to a third. A star schema is the
  other common shape and the search behaves differently on it.
