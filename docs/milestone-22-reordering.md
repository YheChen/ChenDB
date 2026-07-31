# Milestone 22: Reordering across an outer join

Milestone 18 gave the join-order search a rule it could not argue with:

> an outer join runs where it was written, so the search may not reorder across it

Correct, and more than was needed. Consider:

```sql
SELECT a.id FROM a
     JOIN big ON a.k  = big.k
LEFT JOIN tag ON a.id = tag.a_id;
```

`a` and `tag` hold twenty rows each; `big` holds twenty thousand. The `LEFT`
join reads only `a`, so it does not need `big` on its left however the query was
written, and `(a ⟕ tag) ⨝ big` is both legal and half the work. Milestone 18 had
no way to say so and ran `(a ⨝ big) ⟕ tag`.

| | order | time |
|---|---|---|
| Milestone 18 | `a x big LEFT tag` | 99.6 ms |
| Milestone 22 | `a LEFT tag x big` | **49.2 ms** |

Same 20,000 rows, same as SQLite's.

---

## What was built

| File | What changed |
|---|---|
| `engine/planner/physical.py` | `_OuterJoin`, `_outer_constraints`, `_search_constrained`; `_plan_chain` and `_plan_segment` are gone |
| `tests/unit/test_joins.py` | eleven cases, six of them the same query planned both ways |

---

## What an outer join actually requires

PostgreSQL keeps a `SpecialJoinInfo` per outer join carrying `min_lefthand` and
`min_righthand`. The pair exists so a search can **prove** a reordering safe by
set containment instead of refusing all of them because one would be unsafe.
`_OuterJoin` is the same idea with the parts ChenDB needs:

* **`min_right` does not exist.** The `FROM` is a flat chain, so the right of
  every join is a single table and there is nothing to shrink.
* **`min_left`** is the tables the join's own `ON` reads, and it replaces
  "everything written to its left".

Three things keep that honest, and each is a place the rule would otherwise be
wrong rather than merely conservative:

**An empty set falls back to the syntactic one.** An `ON` that reads nothing on
its left has proved nothing about what may be absent.

**`RIGHT` and `FULL` get no freedom at all**, and this is the identity failing
rather than caution. A `LEFT` join NULL-extends the table arriving; a `RIGHT`
join NULL-extends everything accumulated to its left. Move an inner join below
one and it is handed NULLs the written query never showed it:

```
(a ⨝ c) RIGHT JOIN b   an unmatched b gives (NULL, NULL, b)
(a RIGHT JOIN b) ⨝ c   the same b gives (NULL, b), then c's condition on a
                       sees NULL and drops the row
```

**The set is closed under lower outer joins.** Needing `b`, which exists
NULL-extended only because of the join that produced it, means needing that
join's left as well.

## The search

`_plan_chain` used to split the chain into *segments* at each outer join and run
a separate System R search over each. Two tables on opposite sides of an outer
join could never meet, whatever their sizes. It is now one search over every
table, with two rules layered on the dynamic programme:

**A subset must be valid.** Every outer join whose table it contains must have
its `min_left` inside it too. Half an outer join is not a relation: `{b, c}` is
not a set of tables any plan could hold when `b` only exists because `a ⟕ b`
ran. Without this the search would happily cost `b ⨝ c` and then have nowhere
to put the join that made `b`.

**An outer join may run only when it is allowed to.** Its `min_left` must be
present, and every outer join written before it must already have run.

That second condition is the freedom this milestone does *not* take. Commuting
two outer joins is sometimes legal and needs an argument per pair of join types;
the shapes that would benefit are ones the cost model still cannot size well.
It is the inner joins that were freed, which is where the query above lives.

Past `MAX_TABLES_TO_ENUMERATE` the fallback is now **written order** rather than
the old greedy pass. Greedy would need the legality test inside its inner loop
and could still reach a state where no legal pair remains; written order is
legal by construction, and `EXPLAIN` says it gave up.

---

## Verification

**Every query planned twice.** Six queries, run once under this milestone's rule
and once with `min_left` forced back to the syntactic set, and every answer must
be identical. A reordering that changes a row is not an optimisation, and this
is the contract stated as a test rather than as a paragraph.

The fixture is four thousand rows rather than the twenty thousand measured
above, because `execute_script` stops at `DEFAULT_MAX_ROWS` and two plans
returning different ten-thousand-row *prefixes* of the same answer look exactly
like a reordering that changed it. That is a trap this project has now fallen
into twice.

**320,000 generated query pairs agree with SQLite**, over a corpus that since
Milestone 21 builds three tables and links the third over the second's head 40
times in the CI seeds, which is precisely the shape `min_left` reasons about.

**`RIGHT` and `FULL` are pinned by test**, not left to the reader to trust: their
order still ends with the outer join and `EXPLAIN` still says every outer join
needed everything written to its left.

---

## What is still missing

- **Outer joins keep their order relative to each other**, as above.
- **No bushy plans**, still. Left-deep only, so `(a ⨝ b) ⨝ (c ⨝ d)` is not in
  the search space and neither is its outer-join equivalent.
- **`min_left` is computed from the `ON` alone.** PostgreSQL also shrinks it
  using the WHERE and the identities between join types, which recovers
  orderings this does not.
- **The estimator still assumes independence between conjuncts**, which is now
  the thing most likely to make a legal reordering the wrong choice.
