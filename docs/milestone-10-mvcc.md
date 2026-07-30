# Milestone 10: MVCC, locks, and deadlocks

Nine milestones assumed one writer. This one does not, and the whole thing turns
on eight bytes per row:

```
  ┌──────┬──────┬──────────────┬─────────┬─────┐
  │ xmin │ xmax │ null bitmap  │ value 0 │ ... │
  │ u32  │ u32  │  ⌈cols/8⌉ B  │         │     │
  └──────┴──────┴──────────────┴─────────┴─────┘
```

Which transaction made this row, and which one deleted it. That is what turns a
row into a **version**, and a version is what lets a reader answer a question
without waiting for anybody:

```
  txn 5  inserts row A          A.xmin = 5
  txn 5  commits
  txn 7  starts, takes a snapshot        ← sees A
  txn 8  deletes row A          A.xmax = 8
  txn 8  commits
  txn 7  reads again                     ← STILL sees A
```

Transaction 7's snapshot predates 8, so as far as 7 is concerned 8 has not
happened. The row is physically still there. A delete writes eight bytes, it
does not remove anything.

**A reader never waits for a writer.** That is the claim, and
`test_a_reader_does_not_wait_for_a_writer` is it as an assertion.

---

## What was built

```
engine/concurrency/
  snapshot.py   Snapshot, isolation levels, the visibility rule
  locks.py      row locks, the wait-for graph, deadlock detection
```

Plus tuple headers in the record layer, `next_xid` in the meta page (format
version 5), a transaction manager that holds one transaction *per session*
rather than one in total, `Database.vacuum`, three endpoints, and a workspace
with two consoles in it.

---

## No commit log

PostgreSQL needs one (`pg_xact`, formerly CLOG) because it does **not undo**.
An aborted transaction's tuples stay in the heap with their `xmin` set, and the
only way to know they are dead is to look the transaction up. That lookup is
also what makes transaction-id wraparound dangerous, and why anti-wraparound
`VACUUM` exists and has taken production systems down.

ChenDB rolls back by restoring pages, so an aborted transaction's rows are
*physically gone*. **Every row that survives to be read was written by a
transaction that committed.** So the entire commit log collapses into one
number:

```
  xid < frozen_xid   →   committed, and final
```

`frozen_xid` comes from the meta page's `next_xid`, stamped at each checkpoint,
which refuses to run while any transaction is open, so at that instant every
transaction has finished. Only ids at or above it need looking up, and those are
the ones this process is running right now, in memory.

The meta page's copy lags after a crash, because it is only forced at a
checkpoint. Recovery closes the gap: the log carries every id that ran since, so
the horizon on open is `max(meta.next_xid, highest_xid_in_log + 1)`.

This is a simplification bought by a decision two milestones ago, and it is not
free: undoing on abort is what makes rollback cost time proportional to pages
touched, where PostgreSQL's costs nothing. The bill arrives somewhere either way.

**32-bit transaction ids, like PostgreSQL's**, and the wraparound hazard that
famously follows from them cannot bite here for the same reason. The frozen
horizon moves forward at every checkpoint without anybody having to rewrite a
row to make it.

---

## Isolation levels are one branch

```python
if transaction.snapshot is not None and not transaction.isolation.per_statement:
    return transaction.snapshot        # REPEATABLE READ: reuse it
...                                     # READ COMMITTED: take a new one
```

That is the entire mechanical difference between the two levels, and putting it
in one place rather than spreading it through the read path is why the snapshot
is an object at all.

| | Snapshot taken | Consequence |
|---|---|---|
| `READ COMMITTED` | per statement | two identical `SELECT`s can differ, a *non-repeatable read* |
| `REPEATABLE READ` | per transaction | every statement sees the same database |

`test_the_level_is_visible_in_the_snapshot_count` asserts the difference by
counting: three statements under repeatable read take one snapshot, under read
committed take three.

READ COMMITTED is the default, as it is in PostgreSQL, and for the same reason:
repeatable read makes a long-running transaction hold back vacuuming for as long
as it lives.

**Repeatable read here is snapshot isolation**, which is *stronger* than the
standard's REPEATABLE READ. The standard permits phantom rows and this does
not. It is still not serializable: two transactions can each read what the other
is about to overwrite and both commit, producing a state no serial order could.
That is **write skew**, and ruling it out needs predicate locking or PostgreSQL's
serializable snapshot isolation. ChenDB has neither, and says so rather than
implying otherwise by calling the level `SERIALIZABLE`.

---

## Locks: writers only, and one row at a time

A reader never appears in the lock table. Everything in it is a writer
conflicting with a writer, which is the only conflict snapshot isolation cannot
make disappear.

**Row granularity, not page.** ChenDB's undo log works in pages, so page-level
locking would have made two transactions inserting into the same heap page
conflict, which is most inserts, and would have made a second writer useless.
`test_writers_on_different_rows_do_not_conflict` puts two rows on one page
deliberately, so a regression to page locking fails a test rather than quietly
halving throughput.

The consequence is a lock table that grows with rows touched. Real systems
handle that with **lock escalation** (enough row locks on a table become a
table lock), trading concurrency for memory. ChenDB does not escalate;
`MAX_LOCKS_PER_TRANSACTION` is the honest failure instead.

Locks are held until the transaction **ends**, never released early. That is
strict two-phase locking, and it is what stops a second transaction reading a
write that is about to be rolled back.

### Deadlock

Detected, not prevented. The alternatives are worse:

- **Prevention by ordering** needs to know every lock you will take before you
  take the first, which a SQL statement does not.
- **Prevention by timeout** cannot tell a deadlock from a slow transaction, so
  it either kills healthy work or leaves deadlocks sitting.

So: a wait-for graph, and a search for a cycle. Same as PostgreSQL, InnoDB and
SQL Server. PostgreSQL waits a second before even looking (`deadlock_timeout`),
on the reasoning that most waits resolve themselves; ChenDB checks immediately,
because a demonstration nobody waits a second for is a demonstration nobody
watches.

**The victim is the youngest transaction, and the victim is the one that
fails.** Those are two separate decisions and the second is the one worth
noticing: whoever adds the closing edge runs the search, and it would be simpler
to have *that* transaction raise, but then the loser is decided by scheduling
rather than by cost, and "youngest" would be decoration. So the detector marks
the victim, wakes everybody, and each waiter checks on its way out.

```
  txn 3  holds users:4.0        txn 5  holds users:4.1
  txn 5  wants users:4.0   →    waits for 3
  txn 3  wants users:4.1   →    waits for 5      cycle

  → 5 is younger. 5 is rolled back. 3 proceeds.
```

InnoDB picks the transaction that changed the fewest rows, which is a better
proxy for "cheapest" and needs accounting ChenDB does not keep.

---

## Deletes leave versions, and something has to clean up

A delete rewrites four bytes of `xmax`. The slot stays live, the row still
decodes, and the page inspector shows exactly that, which is what PostgreSQL's
page inspector shows too, and why both engines need a vacuum to get the space
back.

```python
db.delete("t", record_id)
db.count("t")          # 4 (what a reader sees
db.version_count("t")  # 5) what is on the page
```

`Database.vacuum` reclaims versions whose `xmax` is below the oldest open
snapshot's `xmin`, meaning nobody left could still be reading the row as it was
before the delete. A single long-running transaction holds that horizon down and
stops vacuum making progress, which is **PostgreSQL's most common "why is my
disk full"**, arrived at by the same mechanism.
`test_vacuum_will_not_reclaim_what_an_open_snapshot_still_needs` pins it.

Manual, not a daemon. A background vacuum in a teaching engine would make row
counts move on their own while somebody was reading them, and *why* space is not
reclaimed at delete time is exactly what this milestone is trying to show.

---

## What it costs

### Per row, on disk

Eight bytes. On a 34-byte row that is 24%, paid by every row whether or not
anything ever reads concurrently. PostgreSQL's `HeapTupleHeaderData` is 23
bytes, carrying a command id, a `ctid` forward pointer to the next version, and
two infomask words of cached flags.

### Per row, on read

| | |
|---|---|
| `read_tuple_header` | 250 ns |
| `visible()` | 83 ns |

The header is read **before** the row is decoded, and that ordering is the only
reason the read cost is bearable: an invisible version costs 250 ns of unpacking
rather than a walk of every column.

### What dead versions do to a scan

```
  5,000 live rows                      1,427 ns per row
  2,500 live + 2,500 dead              2,043 ns per row returned   +43%
  after vacuum                         1,496 ns per row
```

A reader pays for every dead version it walks past. That is the cost of never
blocking a writer, and it is why the visibility check is counted as
`rows_skipped` on the scan operator: a number that grows and never falls is what
an overdue vacuum looks like from the plan view.

### Locking

| | |
|---|---|
| uncontended acquire | 958 ns |
| re-taking one you already hold | 333 ns |

The second matters more than it looks: a transaction updating the same row twice
must not wait for itself, and the fast path for "I have this already" is on the
hot path of every write.

---

## What concurrency this actually has

Worth being exact, because the honest answer is smaller than "MVCC" suggests.

**Statements do not run at the same instant.** The engine still serialises one
at a time behind the database lock. What is new is that several transactions can
be *open* across each other's statements, so a lock held from one statement to
the next blocks somebody else's, and two sessions genuinely interleave,
genuinely conflict, and genuinely deadlock.

That is where every interesting conflict in a real database comes from anyway.
What it is not is parallel execution: making that work would mean every
structure below the transaction manager becoming thread-safe, which is a
different project and would not teach anything this does not.

The lock manager itself *is* thread-safe (waiters block on a condition variable
rather than spinning) because a lock manager that needed external serialisation
would just move the bottleneck.

---

## How this compares

**PostgreSQL** is the closest relative and the source of most of the vocabulary
here. Same 32-bit xids, same `xmin`/`xmax`, same snapshot structure, same
`READ COMMITTED` default, same deadlock detection by wait-for cycle. The
differences are all downstream of one thing: PostgreSQL does not undo. So it
needs CLOG, it needs VACUUM to remove aborted tuples as well as deleted ones, it
needs freezing and it has wraparound. ChenDB's rollback removes the rows, so it
needs none of that, and pays for it with a rollback that costs work.

**MySQL/InnoDB** keeps old versions in the undo log rather than in the heap, and
reconstructs them on demand by walking back. That keeps the table free of dead
rows (no vacuum) at the price of making an old snapshot's reads slower the
longer it lives. It also picks its deadlock victim by rows changed rather than
by age.

**SQLite** has no MVCC at all in rollback-journal mode: one writer excludes all
readers. In WAL mode readers see a snapshot from the last committed frame, which
is snapshot isolation arrived at from a completely different direction, through
the log rather than through the rows.

---

## What the visualizer shows

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ frozen 1 · next 5 · vacuum horizon 3            [ Vacuum ]       │
  ├──────────────────────────────┬───────────────────────────────────┤
  │ ALICE  #4 read committed     │ BOB  #3 read committed            │
  │ snapshot xmin=3 xmax=5       │ snapshot xmin=3 xmax=4            │
  │          active={3,4}        │          active={3}               │
  │ 0 lock(s)                    │ 1 lock(s)                         │
  │ SELECT * FROM users;         │ INSERT INTO users VALUES (9000,…) │
  │ 1 ada · 2 alan · 3 grace     │ inserted 1 row(s)                 │
  ├──────────────────────────────┴───────────────────────────────────┤
  │ LOCKS   users:4.3   held by #3 X      readers blocked: 0         │
  └──────────────────────────────────────────────────────────────────┘
```

Two consoles differing in exactly one thing, the `?session=` on every request.
Bob has inserted and not committed; alice's `SELECT` returned immediately,
without his row and without taking a lock. Her `active={3,4}` is *why*, and it
is printed in her header rather than hidden in a panel, because "why can't I see
the row the other console just inserted" is the question this view exists to
answer.

Three things it is built to make visible:

- **The snapshot as a set.** `xmin`, `xmax` and the active list are the whole of
  the visibility rule, and reading them next to a result set is the shortest
  path to understanding why the result set is what it is.
- **`readers blocked: 0`** as a number, not an absence. A field that is always
  zero looks like an oversight until you know it *must* be.
- **The vacuum horizon.** Open a repeatable-read transaction, delete a row in
  the other console, press Vacuum, and watch it reclaim nothing, then commit
  the first transaction and press it again.

---

## Try it

```bash
python examples/milestone10_mvcc.py
```

```bash
python -m pytest tests/unit/test_mvcc.py tests/integration/test_concurrency_api.py -v
```

---

## What is still missing

- **Serializable isolation.** Write skew is possible. Ruling it out needs
  predicate locking or SSI.
- ~~**No `UPDATE`.**~~ Fixed in [Milestone 11](milestone-11-dml.md), which is
  where version chains actually get a second link. At this point the engine has
  `INSERT` and `DELETE` only, so every version is one deep.
- **No parallel statement execution**, as above.
- **No lock escalation**, so a very wide transaction runs out of lock table
  rather than degrading.
- **No autovacuum**, deliberately.
- **Indexes do not carry visibility.** An index entry says "a version of this
  key lives here", not "a visible one does", so an index scan re-checks the
  heap. That is exactly why PostgreSQL needs a visibility map to make
  index-only scans possible, and ChenDB has no such thing.
