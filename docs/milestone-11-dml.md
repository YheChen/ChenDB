# Milestone 11: `UPDATE` and `DELETE`

Ten milestones could write a row and take it away again. What was missing is the
case in between: a row that *changes*. It sounds like the smallest of the three
and it is the largest, because of one sentence:

> **An update is a delete and an insert.**

```
  before          slot 3   xmin=7  xmax=0   (1, 'ada', 40000)

  UPDATE staff SET pay = pay + 5000 WHERE name = 'ada'

  after           slot 3   xmin=7  xmax=9   (1, 'ada', 40000)   ← still readable
                  slot 8   xmin=9  xmax=0   (1, 'ada', 45000)   ← the new version
```

Nothing was edited. The old version got eight bytes of header written to it and
the new one was appended somewhere else. Everything surprising in this milestone
. The index cost, the Halloween problem, the two-deep version chain, the reason
a row's *address* is not stable, follows from that.

---

## What was built

```
engine/parser/ast.py       UpdateStatement, DeleteStatement, Assignment
engine/parser/parser.py    two grammar rules, and UPDATE/DELETE out of _NOT_YET
engine/executor/binder.py  bind_update, bind_delete, identity_projection
engine/executor/engine.py  _locate_rows, _execute_update, _execute_delete
engine/database.py         update_many, delete_many, _update_one
```

Plus `EXPLAIN` on both, a `version_count` on the catalog's storage panel, and
two new walkthroughs in the concurrency workspace.

Grammar:

```
update     := UPDATE ident SET assignment { ',' assignment } [ WHERE expr ]
assignment := ident '=' expr
delete     := DELETE FROM ident [ WHERE expr ]
```

---

## Finding rows is a query, so it uses the planner

A `DELETE` has two halves and only one of them has more than one right answer:

```
  DELETE FROM staff WHERE pay = 75000
  │
  ├── which rows?     ← a query. Sequential scan, or descend an index?
  └── then what?      ← one thing, always: set xmax, unlink from every index
```

The first half goes through exactly the same path a `SELECT` does, bind, build
a logical plan, rewrite, enumerate access paths, cost them, choose. That is not
tidiness. Without it, deleting one row out of a million reads all million:

```
Delete on staff
  PhysicalIndexScan  index=pay_idx pay = 75000  (cost=2.8 rows=1)
  then, per row: set xmax, remove it from every index

Statistics: 16 rows, 1 pages
Alternatives considered:
       Sequential scan of staff  cost=7.9 rows=16  [2.8x the cost of the chosen plan]
    -> Index scan on pay_idx (pay = 75000)  cost=2.8 rows=1
```

The second half is *not* planned, and gets one honest line saying what it does
rather than a fabricated operator with an invented cost. That matches how
`INSERT` has been handled since Milestone 3: a statement with exactly one
execution strategy does not need a plan node to say so.

`EXPLAIN ANALYZE` on either really performs the change, as it does in
PostgreSQL, and the usual advice applies, wrap it in a transaction you mean to
roll back.

---

## The Halloween problem

```
  UPDATE salaries SET pay = pay * 1.1 WHERE pay < 50000
```

Every raise leaves the row still under 50,000. If the scan is still running
while the writes happen, it reaches the new versions too. They match, so they
are raised again, and again, until they escape the predicate. A 10% raise turns
into "everybody now earns exactly 50,000".

It was found at IBM in 1976, on 31 October, and the name stuck.

ChenDB is exposed to it for a reason worth naming precisely: an MVCC update
**inserts**, and a transaction always sees its own writes, so the new version is
genuinely reachable by the scan that produced the old one. This is not
hypothetical here, remove the materialisation and
`test_an_update_that_keeps_matching_its_own_predicate_still_runs_once` fails.

The fix is to drain the row source into a list before writing anything:

```python
record_ids = [...]        # the whole matched set, first
for record_id in record_ids:
    ...                   # then, and only then, write
```

Three engines, three ways to the same place:

| | how it avoids Halloween |
|---|---|
| **ChenDB** | materialise the matched set before writing |
| **PostgreSQL** | the scan will not return a tuple its own *command* wrote, `cmin`/`cmax`, a command counter inside the transaction |
| **SQL Server** | inserts an explicit `Eager Spool` operator into the plan, which is the ChenDB approach with an operator around it |

PostgreSQL's is the cheapest, because it needs no buffer. It also needs a second
counter in every tuple header, which is eight more bytes per row on a structure
this project deliberately kept to eight total.

The cost of buffering is memory proportional to rows matched. Rather than cap it
silently, a statement that matches more rows than the ceiling **refuses to run
at all**, changing the first 10,000 and reporting success is the one outcome
worse than failing.

---

## What an update costs an index

Every index on the table is rewritten, **including indexes on columns the update
did not touch**:

```
  CREATE INDEX name_idx ON staff(name);        -- not on `pay`
  UPDATE staff SET pay = 2 WHERE name = 'alan';

  name_idx before:   'alan' → slot 1
  name_idx after:    'alan' → slot 9           ← rewritten anyway
```

The value did not change; the *address* did. An index entry is a key and a
`RecordId`, and the live row is somewhere else now.

So an update to a table with four indexes is four B+ tree deletes and four B+
tree inserts on top of two heap writes. Measured on 2,000 rows with two indexes:

| | µs/row |
|---|---|
| `UPDATE` | 185 |
| `DELETE` | 86 |

Roughly double, for a structural reason rather than an incidental one: the
delete writes eight bytes of header and removes an index entry; the update does
all of that *and* encodes a whole new row *and* puts an entry back.

**This is exactly what PostgreSQL's heap-only tuples exist to avoid.** If no
indexed column changed *and* the new version fits on the same page, PostgreSQL
chains the old version forward to the new one and leaves every index alone. The
index still points at the old tuple, and a scan that lands there follows the
chain. ChenDB has no HOT, so it pays in full every time. The condition is worth
reading twice, because it is why "leave free space on the page" (`fillfactor`)
is a real tuning knob in PostgreSQL and not superstition.

---

## Assignments all see the old row

```sql
UPDATE staff SET a = b, b = a WHERE name = 'ada'
```

swaps the two columns. It does not assign `b` to `a` and then `a` (now `b`) back
to `b`. Every right-hand side is evaluated against the row **as it was**, and
the results are applied together.

Every SQL engine does this, and it is the single thing about `SET` that surprises
people arriving from a procedural language. Mechanically it falls out of the
implementation for free. The old row is already in hand as a tuple, and each
assignment is evaluated against that tuple rather than against the list being
built, which is a good sign the semantics were chosen well in 1974.

---

## Two writers, and one distinction that matters

```
  alice: locate rows          → slot 3
  bob:   UPDATE slot 3        → slot 3 now has xmax = bob
  alice: UPDATE slot 3        → ?
```

The answer depends entirely on **whether bob has finished**, and getting that
wrong is how a lost update becomes invisible:

| bob's state | what alice must do |
|---|---|
| committed | give up on this row and *say so* |
| still open | **wait**, bob may yet roll back, in which case nothing happened |
| rolled back | proceed; the page was restored and the `xmax` went with it |

The middle row is the one that is easy to get wrong, and this milestone found it
already wrong. `_delete_one` had shipped in Milestone 10 checking only
`header.deleted`, so a writer gave up the moment it saw a dead version, no
matter whose. Two sessions could each conclude the other had won and neither
change anything, and a rolled-back delete would silently swallow somebody else's
update.

`Database._claim_row` is the fix, and the order in it *is* the algorithm: ask
whether the deleter has **finished** (not whether the row is dead), then take
the lock (which blocks if it has not) then re-read, because waiting means the
world moved on. A transaction is finished exactly when it is not in
`running_ids()`; no commit log is needed, because a rollback removes its work
physically, so any `xmax` still on the page from a finished transaction is a
committed one.

When bob really has committed, ChenDB gives up and reports it:

```
updated 3 row(s) in staff; 1 of the 4 matched were changed by another
session first and were skipped
```

PostgreSQL does not give up there. Under `READ COMMITTED` it follows the
`t_ctid` forward pointer to the new version, **re-evaluates the `WHERE` clause
against it**, and updates that one if it still matches, EvalPlanQual, a
substantial amount of code for a case most applications never think about.
ChenDB's answer is smaller and not wrong. What it must never do is silently
overwrite bob, and reporting only "updated 3" would make a lost update look
like a clean one.

### Why none of this can be shown in the browser

`ManagedDatabase` gives each database one `RLock` and holds it for a whole
request, so **a statement's locate-and-mutate is atomic with respect to the
other console**. Neither of the two interesting outcomes is reachable from two
browser tabs:

- The *skip* needs alice to commit between bob locating a row and bob writing
  it. There is no such moment; bob's whole statement runs under the lock.
- The *wait* needs bob to block on a row lock, which he would do while holding
  the engine lock, so alice could never commit to release him. It could only
  end in the five-second timeout, with the UI frozen throughout.

Milestone 10 shipped a "Two writers collide" button promising the second one
would wait. It never ran at all, because it loaded `DELETE`, which the parser
refused. Had it run, it would not have shown a wait either.

So it is gone, replaced by two walkthroughs that demonstrate something true, and
the conflict paths are asserted where they can be: two sessions on one handle in
`tests/unit/test_mvcc.py`. Removing a button that would have to lie is the same
rule this project has followed since Milestone 1. A feature that is not there
is absent, not stubbed.

---

## Three things that were already broken

None was introduced here. All three were found *by* this milestone, because an
update exercises paths a delete-only engine barely touched. The write-write
conflict above was a fourth.

**The catalog was counting versions as rows.** `heap.count()` counts slots, so
the table panel's `row_count` (documented as "live rows") had been including
dead versions since Milestone 10. A delete is rare enough in a demo that nobody
noticed; an update makes the panel disagree with `SELECT COUNT` immediately. Now
`row_count` is what a reader sees and `version_count` is what is on the pages,
with the gap being exactly what `Vacuum` would reclaim.

**The write-ahead log was quadratic in records per transaction.** `next_lsn` is
read once per append, and it computed `sum(len(chunk) for chunk in buffer)`
every time, O(n) per append, O(n²) per transaction. A 2,000-row `UPDATE` spent
**8.5 of its 9.8 seconds inside that `sum`**, doing 65 million length
computations. Keeping a running total is a four-line change and takes the same
statement to 1.2 seconds. It had been there since Milestone 9 and nothing before
this buffered thousands of records in one transaction.

**A named session's implicit transaction was never committed.**
`Database.transaction()` asked `TransactionManager.active` (which is the
*default* session's transaction) decided it did not own the one it had just
opened for `carol`, and left it running. So `POST /query?session=carol` with a
bare `INSERT` reported success, and the row stayed invisible to everyone
including a later reader in the same session's next request, while a row lock
and the vacuum horizon were held until the process ended.

It survived Milestone 10 because the only thing that exercises a named session
is the two-console workspace, and both of its working walkthroughs had the
session press `BEGIN` first.

A profiler found the quadratic in about ninety seconds; the docstrings had been
confidently describing an append as O(1) for two milestones. The other two took
a demo button that had to work.

---

## What the visualizer shows

```
  [ A reader does not wait ]         [ Two levels, two answers ]
  [ One row, two versions ]          [ A writer locks, a reader does not ]
  [ A rollback leaves nothing behind ]
```

Three of those are new, and each shows something that could not happen before
this milestone.

**One row, two versions** is the Milestone 10 payoff that Milestone 10 could not
demonstrate. Bob updates without committing; alice's `SELECT` returns the old
values instantly, and the table panel reads `rows 3 · versions 6` until bob
commits and `Vacuum` removes the losers.

**A writer locks, a reader does not** is the whole claim of MVCC in one panel:
bob holds six exclusive row locks (two per row, the old version and the new)
while alice holds zero, reads immediately, and `readers blocked` stays 0.

**A rollback leaves nothing behind** shows the design decision that removes the
commit log. Versions climb while bob's transaction is open and drop back on
`ROLLBACK`, rather than lingering as dead weight for a vacuum.

Every walkthrough (and the writer console's own default statement) is now
built from the real schema through `demoRows.ts`, which exists because a
demo button has now shipped wrong three times. The one this milestone was sent
to fix loaded `DELETE FROM … WHERE id = 1` into both consoles at a point when
the parser refused `DELETE` outright, *and* hardcoded a column named `id` that
the open table might not have.

---

## Try it

```bash
python examples/milestone11_dml.py
```

```bash
python -m pytest tests/unit/test_dml.py tests/unit/test_mvcc.py -v
```

---

## What is still missing

- **No `RETURNING`.** A statement reports counts, not rows. PostgreSQL's
  `UPDATE … RETURNING *` needs the mutation to be an operator that emits rows,
  which is the same refactor that would make `INSERT … SELECT` work.
- **No EvalPlanQual**, as above.
- **No `UPDATE … FROM` / `DELETE … USING`.** Both need a second row source, and
  there are no joins yet.
- **No HOT.** Every index is rewritten on every update.
- **No `UPDATE` of a primary key checked for uniqueness**, because `PRIMARY KEY`
  is still metadata rather than an implied unique index, the same gap
  Milestone 8 ran into.
- **The matched set is buffered in memory**, so the row ceiling is a hard
  refusal rather than a streaming limit.
- **No `TRUNCATE`.** `DELETE FROM t` with no predicate writes a version per row
  and leaves every one of them for the vacuum; `TRUNCATE` would drop the pages.
  For a large table the difference is minutes against milliseconds.
