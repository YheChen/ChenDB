# Milestone 23: Subqueries, and the one shape that is a constant

```sql
SELECT id FROM orders WHERE total > (SELECT AVG(total) FROM orders);
```

The parser has refused `(SELECT …)` for twenty-two milestones. It no longer
does, for the shape where the answer is simple and the simplicity is not a
shortcut.

An **uncorrelated** subquery names nothing outside itself. It therefore depends
on no row of the query around it, and has **one value for the whole statement**
however many rows that statement scans. So it is run once and its value
substituted, before binding:

```
WHERE total > (SELECT AVG(total) FROM orders)
WHERE total > 200
```

That is not an optimisation of a more general mechanism. For this shape it is
the entire semantics, and doing it before binding is what makes everything
downstream work unchanged. The planner, the index matcher and the cost model
were all written against `column <op> literal`, and after folding that is
exactly what this is: `total = (SELECT MAX(total) FROM orders)` uses the index
on `total`, which it could not do if the subquery survived into the plan.

---

## What was built

| File | What changed |
|---|---|
| `engine/parser/ast.py` | `ScalarSubquery`, the one node in this grammar that holds a whole statement |
| `engine/parser/parser.py` | `_primary` recurses when `(` is followed by `SELECT` |
| `engine/executor/engine.py` | `fold_subqueries`, called before binding by both `_execute_select` and `_execute_explain` |
| `tests/unit/test_subqueries.py` | new, thirteen cases |
| `tests/differential/generator.py` | a `scalar_subquery` predicate shape, 59 in the CI seeds |

---

## What it refuses, and why each refusal is a decision

**More than one row** is an error, PostgreSQL's. There is no defensible answer:
taking the first would make the query depend on physical order, which is the
kind of wrong that looks right until a `VACUUM` moves a page.

**More than one column** is an error, and `SELECT *` counts as more than one
even when the table currently has one column. Refusing on shape rather than on
today's count means an `ALTER TABLE` cannot turn a working query into a wrong
one.

**Zero rows is NULL**, not an error. `x = NULL` is UNKNOWN for every row, so the
query returns nothing, which is the answer. An error here would mean a
reasonable query failing because a table happened to be empty.

**A correlated subquery is refused by name.**

```sql
SELECT o.id FROM orders o
WHERE o.total = (SELECT MAX(i.total) FROM orders i WHERE i.city = o.city);
```

This is a different feature with a different implementation: a join, or one
execution per outer row. Running it per row without saying so would turn a query
somebody wrote into a plan nobody would have chosen.

It would also have been refused *by accident*, since `o.city` does not resolve
inside a subquery whose `FROM` is `orders i`. The binder would have said "no
column named 'city'", which is an hour of somebody's life spent looking at a
column that plainly exists. So correlation is detected first, by comparing the
qualifiers a subquery uses against the aliases of the query around it, and the
error says what is actually wrong.

An unqualified column that really does not exist still gets "no column named",
and `SELECT MAX(orders.total) FROM orders` inside a query over `orders` is not
correlated, because the qualifier names the subquery's own `FROM`.

---

## Verification

**320,000 generated query pairs agree with SQLite**, with a scalar subquery in
59 of the 1,024 CI-seed queries. The generator emits `COUNT(*)` and `MIN`/`MAX`
over an INTEGER column, which are the forms where both engines agree on the
result's *type* as well as its value.

**Thirteen hand-written cases**, five of them about what is refused, and one
about the thing folding buys: after substitution the predicate reaches an index,
which is checked by planning it rather than by asserting it.

---

## What is still missing

- **Correlated subqueries.** The large half of this feature. `EXISTS`,
  `NOT EXISTS` and a correlated scalar are all the same underlying problem, and
  the right implementation is decorrelation into a semi-join or an anti-join
  rather than re-execution.
- **`IN (SELECT …)`** and **`IN (…)`**, both still refused by the parser. The
  list form needs no subquery machinery at all and is the smaller of the two.
- **A subquery in `FROM`**, a derived table. That needs the binder to build a
  scope from a query's output columns rather than from a catalogue entry, which
  is the same machinery a view would need.
- **`UNION`, `INTERSECT`, `EXCEPT`.**
- **The subquery is not costed.** `EXPLAIN` shows the folded literal and says
  nothing about the work that produced it, which is honest but incomplete: the
  subquery is real work and a plan that hides it is understating itself.
