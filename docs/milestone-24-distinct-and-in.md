# Milestone 24: `DISTINCT` and `IN`, and two NULLs that disagree

Two features with nothing to do with each other in the engine, shipped together
because they are the two things a visitor types first and could not do:

```sql
SELECT DISTINCT city FROM users;              -- "not implemented yet"
SELECT * FROM users WHERE id IN (1, 2, 3);    -- "not implemented yet"
```

They also turn out to teach the same lesson from opposite ends. Each is a place
where SQL's idea of equality is not the one a programmer brings to it, and the
two disagree with each other:

* **`DISTINCT` treats two NULLs as the same**, so one row survives, even though
  `NULL = NULL` is unknown everywhere else in the language.
* **`NOT IN` treats a NULL in its list as poison**, so `x NOT IN (1, NULL)` is
  never TRUE, however unrelated `x` is to either value.

Both are the standard's, and both fall out of definitions rather than needing
rules of their own.

---

## What was built

| File | What changed |
|---|---|
| `engine/parser/ast.py` | `SelectStatement.distinct`, and an `InList` node |
| `engine/parser/parser.py` | `DISTINCT` after `SELECT`; `IN` and `NOT IN`, with one token of lookahead |
| `engine/executor/operators.py` | `Distinct`, and `distinct_key` |
| `engine/planner/logical.py`, `physical.py` | `LogicalDistinct`, `PhysicalDistinct`, `distinct_cost` |
| `engine/optimizer/cost.py` | `IN` estimated as a union of equalities |
| `engine/optimizer/nullability.py` | Milestone 19's analysis learns `InList` |
| `tests/unit/test_distinct_and_in.py` | new, nineteen cases |

---

## `IN` is a union of equalities, exactly

```
x IN (a, b)       ≡  x = a OR x = b
x NOT IN (a, b)   ≡  x <> a AND x <> b
```

Not "approximately". That equivalence *is* the definition, and every surprising
case follows from it rather than being coded:

| | |
|---|---|
| `NULL IN (1, 2)` | NULL, because every comparison is unknown |
| `2 IN (2, NULL)` | **TRUE**, because `TRUE OR unknown` is TRUE |
| `x NOT IN (1, NULL)` | **never TRUE**, because it means `x <> 1 AND x <> NULL` |

The last is the one that catches everybody, and it is not a bug in anybody's
engine. "Is x different from a value I do not know" has no answer, so the row
is neither kept nor rejected: it is unknown, and a `WHERE` keeps only TRUE.

### Why it is a node and not sugar

The desugaring above is exact, NULLs included, so the parser could emit an `OR`
chain and delete `InList` entirely. It does not, because the AST view is meant
to show the query somebody wrote. An error span pointing at an `OR` nobody typed
costs a reader more than the node costs the evaluator, which is one `case`.

`IN (SELECT …)` is refused **by name**, and the message says which of the two
forms this parser understands. It is a semi-join, not a list, and evaluating it
as one would mean materialising the subquery per row, which is a plan nobody
would choose. That belongs with correlated subqueries.

### It was already estimated well, by accident

Milestone 20 gave every column a most-common-values list with exact counts. A
union of equalities is a sum of their selectivities, so `IN` inherits that for
free, and on a column small enough for its list to be complete the estimate is
a count rather than a guess:

| | estimated | actual |
|---|---|---|
| `city IN ('london', 'ny')` | 334 | 334 |
| `city NOT IN ('london')` | 333 | 333 |
| `age IN (7)` | 10 | 10 |

The alternative was `DEFAULT_INEQ_SELECTIVITY`, a third of the table, for every
`IN` ever written.

### And Milestone 19 had to learn one more node

`b.id IN (1, 2)` cannot be TRUE about a row an outer join invented, for exactly
the reason `b.id = 1` cannot, so it should prove the join inner. The
null-rejection analysis returns "could be anything" for a node it does not
recognise, which is *safe* and would have quietly stopped rewriting. Teaching it
`InList` is four lines and a test that fails without them.

---

## `DISTINCT` is streaming, and that is the interesting choice

The lazy implementation is a sort that drops equal neighbours. It is correct,
it needs no new operator, and it is wrong here for one reason: a sort is
**blocking**. It has to see every row before it emits one, so `SELECT DISTINCT
city FROM t LIMIT 2` would read the whole table.

`Distinct` hashes instead. The first row comes out immediately, a `LIMIT` above
it really does stop the scan early, and the plan shows no sort:

```
PhysicalLimit  2
  └─ PhysicalDistinct
       └─ PhysicalProject  city
            └─ PhysicalSeqScan  table=t
```

What it holds instead of a buffer is a set of every distinct row seen, which is
the same memory a hash aggregate would hold for the same keys. That is the trade,
and it is the right way round for a `LIMIT`.

**Above the projection**, because `DISTINCT` deduplicates what the query
*returns*: `SELECT DISTINCT city` over a table with a unique id is three rows,
not seven, which is only true if the id is dropped first.

### Two values are the same when SQL says so

`distinct_key` carries each value's type alongside it, and that is not
fastidiousness. Python hashes `True` and `1` alike, and `1` and `1.0` alike, so
a plain `set` of row tuples would fold a BOOLEAN row into an INTEGER one and
lose it. No test of the SQL surface would notice while a fixture happened to
hold only one of the two types.

`-0.0` and `0.0` are deliberately the *same* key: they compare equal in SQL, so
emitting both would be a duplicate. This is the same fold the differential
oracle makes, and it got there by reporting a false divergence first.

---

## Verification

**320,000 generated query pairs agree with SQLite.** The generator now emits
`DISTINCT` on a fifth of plain selects and an `IN` list in a fifth of
predicates, a quarter of those with a NULL in the list on purpose. In the CI
seeds that is 119 distinct queries, 70 `IN` lists and 21 with a NULL, which is
the combination no hand-written case would have thought to put under an outer
join.

**Nineteen hand-written cases**, weighted at the NULL rules, plus the two
integration points that would have failed silently: the estimate, and Milestone
19's rewrite.

**Five guards fired, as designed.** Both features were listed as unimplemented
in five places, and every one went red the moment they worked:
`test_valid_sql_that_is_not_implemented_says_so` twice,
`test_unsupported_sql_is_distinguished_from_a_syntax_error`, the demo
catalogue's "not implemented yet" slot, and `examples/milestone2_parser.py`,
whose list of deliberately-failing statements is checked by `make examples` in
CI. The fifth was found by CI rather than by looking, and its own comment says
this has happened twice before.

That slot has now had **four** occupants: `ORDER BY` until Milestone 13,
`LEFT JOIN` until 18, `DISTINCT` until this one, and `LIKE` now. Three for three
on catching its own staleness on the very next run is the best argument for
keeping it. An example of what an engine cannot do is a claim with a shelf life.

---

## What is still missing

- **`IN (SELECT …)`**, a semi-join, with correlated subqueries.
- **An `IN` list cannot use an index.** One equality can, and a list of them
  needs a multi-range index scan this engine has no operator for. The plan falls
  back to a filter and says so.
- **`COUNT(DISTINCT x)`** is a different feature: `DISTINCT` inside an aggregate
  rather than over the output row.
- **`SELECT DISTINCT ON (…)`**, PostgreSQL's extension, is not standard and is
  not here.
- **The distinct estimate is a tenth of the input, floored at one.** The honest
  number needs the distinct count of a *combination* of columns, which nothing
  collects: Milestone 20 counts per column, and the pairs are not derivable from
  those without assuming independence, which is what that model is already worst
  at. Nothing downstream depends on it, so a weak guess is stated rather than
  dressed up.
- **`LIKE` and `BETWEEN`**, still refused by name.
