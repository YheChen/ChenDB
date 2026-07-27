# Milestone 8 — transactions

Before this milestone, a statement that failed half-way left the half behind.

```sql
INSERT INTO users VALUES (1, 'ada'), (2, 'alan'), (1, 'dup');
--                        ^ written   ^ written    ^ fails
-- Milestones 1-7: rows 1 and 2 are in the table. Row 3 is not.
-- Milestone 8:    nothing is in the table.
```

The mechanism is one page-sized snapshot per page, taken the first time that
page changes:

```
  BEGIN
    INSERT INTO users  VALUES (1, 'ada')      page 4 changes  →  before-image
    INSERT INTO orders VALUES (1, 1, 9.99)    page 9 changes  →  before-image
    INSERT INTO users  VALUES (1, 'dup')      fails
  ROLLBACK
    page 9 ← its before-image
    page 4 ← its before-image                 the database is as it was
```

That is it. There is no log of *what happened*, only of *what the bytes were*.

---

## What was built

```
engine/transaction/
  undo.py      UndoLog: page id → before-image, first write wins
  manager.py   TransactionManager: begin/commit/rollback, the write hook
```

Plus a single hook in the pager, three SQL statements, four HTTP endpoints, and
a workspace. The engine's other subsystems — the heap, every index, the catalog,
the buffer pool — were **not modified**, and the next section is why.

---

## Physical undo, and what it bought

An undo log can record what a change *meant* (logical) or what the page *was*
(physical). ChenDB records the page.

A logical undo has to know how to reverse every operation. Insert a row → delete
it. Split a B+ tree node → merge it back and fix up the parent separator, and
the parent's parent if that cascaded. Create a table → delete two catalog rows,
free a heap page, decrement `next_object_id`, and invalidate the caches. Each
inverse is a separate piece of reasoning, and each is a separate chance to get it
wrong in a way that only shows up during a rollback nobody tested.

A physical undo knows nothing. It restores bytes.

The payoff is measurable in the diff: **`CREATE TABLE` became atomic with zero
lines changed in `engine/catalog/`.** Creating a table writes rows into two
system tables, allocates a heap page, and bumps a counter in the meta page. The
undo log saw four pages change and kept four snapshots. It never learned what a
table is.

The same is true of every B+ tree operation. A root split touches three pages,
so rollback restores three pages, and `engine/index/` does not mention
transactions anywhere.

### What it costs

Two things, both real:

**Granularity.** The unit is a page, so two transactions changing different rows
on the same page conflict at the page level. With one writer that costs nothing,
which is the only reason this is affordable — Milestone 10's MVCC is where
row-level versioning has to arrive.

**Log size.** A logical record for one insert is a few dozen bytes; a physical
one is 4,096. That sounds ruinous until you notice what it is *per*:

```
  20,000 rows inserted inside one transaction

    page writes seen      20,352
    before-images kept        91      ← 223x fewer
    undo log                 364 KiB
    rollback                 111 us
```

The undo log grows with **distinct pages touched**, not with rows written,
because a page is captured once and every later write to it is free. A logical
log would have held 20,000 records. This one held 91 snapshots.

That is `first-write-wins`, and it is what makes the physical choice practical
rather than merely simple:

```python
def capture(self, page_id, image, reason=""):
    if page_id in self._captured:
        return False          # already have this page's "before"
    ...
```

PostgreSQL's full-page writes use exactly this trick for exactly this reason:
the first change to a page after a checkpoint writes the whole page to the WAL,
and subsequent ones write only the delta.

### The bound

`MAX_UNDO_BYTES` caps the log at 64 MiB — about 16,000 pages at 4 KiB. Past that
the transaction stops capturing and can no longer be rolled back, which is
reported rather than discovered. A real system spills undo to disk; that needs
somewhere durable to put it, which is Milestone 9.

---

## Implicit transactions

A statement run with no transaction open gets one anyway:

```python
db.transactions.begin(implicit=True)   # opened by the engine
...                                     # the statement runs
db.commit()                             # committed when it finishes
```

So the multi-row `INSERT` at the top of this page is atomic without anybody
typing `BEGIN`. This is autocommit, and it is why the transaction timeline in the
visualizer is mostly full of transactions the user never asked for — a fact the
UI labels explicitly, because a timeline that hid it would look like the user had
been running `BEGIN` constantly.

`execute_script` wraps a **whole script**, not each statement. That is
deliberately not the SQL standard, and it is what `execute_script`'s docstring
has promised since Milestone 3: half-applied setup is rarely what anyone wants.
A caller that needs per-statement autocommit sends one statement at a time, or
passes `atomic=False`.

`BEGIN` inside an implicit transaction **adopts** it rather than nesting, so a
script reading `BEGIN; …; COMMIT;` behaves the way it looks. Real nesting needs
savepoints, which ChenDB does not have — and pretending to nest without them
would mean an inner "rollback" that silently rolled back the outer transaction
too.

### A lone `BEGIN` keeps the transaction open

`execute_script` auto-commits only a transaction that is *still implicit*. Once a
`BEGIN` in the script has made it explicit, the client owns it:

```sql
-- request 1
BEGIN;                                  -- transaction stays open
-- request 2
INSERT INTO users VALUES (9, 'grace');  -- joins it
-- request 3
ROLLBACK;                               -- the client ends it
```

Without that rule, `BEGIN;` alone would be a silent no-op — the user types it,
sees success, and has no transaction. PostgreSQL behaves the same way with the
same text in one simple-query message.

---

## The failed state

A statement that raises inside an explicit transaction does *not* end it. The
transaction is marked `failed`, and from then on only `COMMIT` and `ROLLBACK`
are accepted:

```
ERROR:  current transaction is aborted, commands ignored until end of
        transaction block
```

That is PostgreSQL's rule and PostgreSQL's wording, and it exists because the
alternative is worse. `execute_script` unwinds a transaction *it* opened, but a
transaction the client opened in an earlier request belongs to the client — so
without a failed state, an error would leave the partial work in place and a
later `COMMIT` would keep it. **Half a transaction, committed, is the one outcome
this milestone exists to prevent.**

`COMMIT` on a failed transaction rolls back instead, and says so. Again
PostgreSQL's behaviour — `COMMIT` in an aborted block prints `ROLLBACK` — and the
safe direction: the caller never gets half a transaction, and never gets stuck
in one either.

---

## One hook, and where it goes

Every page write in the engine funnels through `Pager._write_at`. That makes it
the only place a before-image has to be captured:

```python
def _write_at(self, page_id, raw, reason="", *, capture=True):
    ...
    if capture and self._on_before_write is not None:
        self._on_before_write(page_id, lambda: self._pool.fetch(page_id), reason)
```

The current image is passed as a **callable, not bytes**. Reading a page to
snapshot it is wasted work whenever the page is already captured, and thanks to
first-write-wins that is the overwhelming majority of writes — 20,251 of the
20,352 above.

Measured, against a 41 ns loop floor:

```
  before_write, no transaction open      42 ns    a None check
  before_write, page already captured    84 ns    a set lookup
```

An insert costs ~13 µs, so the hook is under 1% either way. **The event system's
rule applies here too: the machinery has to be nearly free when it is not doing
anything**, because it sits on the path of every write in the engine.

### Two places `capture=True` would be wrong

**A freshly allocated page** has no "before" — it is not in the file yet, and
trying to read it raises `CorruptDatabaseError: short read`. Allocation passes
`capture=False`, and instead extends the file at allocation time so the page
exists before the meta page claims it does.

**A rollback itself** writes before-images back. Going through the hook would try
to capture the page it is in the middle of restoring. `restore_page` bypasses it.

---

## Rollback is more than restoring pages

Two pieces of engine state live in memory rather than being re-read from a page,
and both would otherwise survive a rollback and describe a database that no
longer exists:

```python
transaction = self._transactions.rollback(self._restore_page)
self._pager.reload_meta()        # page_count, next_object_id
self._catalog.invalidate()       # a rolled-back CREATE TABLE
self._statistics.invalidate()    # row counts from rows that are gone
```

The catalog one is the sharp edge. Without `invalidate()`, a rolled-back
`CREATE TABLE` leaves the engine cheerfully serving a table whose heap page has
been overwritten — every query against it reading whatever was there before.

Rollback costs about a microsecond per page:

```
  rollback of   100 rows:   4 pages    10.3 us
  rollback of 1,000 rows:   8 pages    11.0 us
  rollback of 5,000 rows:  25 pages    27.1 us
```

Nearly all of that is fixed overhead — the marginal cost is ~1 µs per page,
which is one memory copy plus one write through the buffer pool.

---

## The hard part: the buffer pool

Milestone 7's pool writes a dirty page to disk when it evicts one, and it has no
idea whether the transaction that dirtied it has committed. In ARIES vocabulary
that is a **steal** policy, and steal is what makes crash recovery need a log.

ChenDB allows steal. The consequence is precise, and stating it precisely is more
useful than blurring it:

- **Rollback in this process is always correct**, evicted or not. The
  before-images are in memory; writing one back through the pool re-admits the
  page with the old bytes, whether the page was still resident or had been
  written out an hour ago.
- **A crash mid-transaction is not atomic.** Whatever the pool happened to evict
  is on disk, the undo log died with the process, and nothing on disk says a
  transaction was ever open.

So: **atomic against errors, not against power loss.** `tests/recovery/` pins
that down in both directions rather than leaving it to the prose.

### Why pinning was rejected

The obvious fix is to pin dirty uncommitted pages so the pool cannot steal them
— a no-steal policy. It was considered and rejected, because *it does not buy
crash atomicity either*.

No-steal keeps uncommitted pages out of the file, but a crash **during the commit
flush** still leaves a partial transaction on disk, and nothing there
distinguishes that from a complete one. Making commit atomic needs a commit
*record*: one small durable write that says "everything before this counts". That
is a write-ahead log, and it is Milestone 9.

Pinning would have cost pool exhaustion on large transactions — a transaction
touching more pages than the pool has frames could not proceed — in exchange for
nothing.

### A real bug this found

Writing the crash tests surfaced a **Milestone 7 write-ordering bug**, not a
Milestone 8 one.

`allocate_page` used to bump `meta.page_count` and let the pool write both pages
whenever it felt like it. With 128 frames that is invisible. With 4 frames, the
meta page can be evicted *before* the pages it references, leaving a file
physically shorter than its own page count claims — and the length check
correctly refuses to open it.

Milestones 1–6 had the right ordering for free, because they wrote every page
immediately. The pool removed that guarantee silently. The fix restores it:

```python
page_id = self._meta.page_count
self._meta.page_count += 1
self._extend_file_to(self._meta.page_count)   # ftruncate, one syscall
```

A crash between the `ftruncate` and the page's flush leaves a zero-filled page,
which fails its checksum when read — detected, not silently returned as
plausible garbage. That is the same contract every other torn page has here, and
`test_a_page_the_crash_never_flushed_is_detected_not_returned` asserts it.

---

## Why one transaction at a time

`TransactionManager` holds exactly one. The database-level write lock already
serialises callers, so there is nothing for a concurrency-control scheme to
arbitrate — inventing a lock manager for a single writer would be structure with
no user. Milestone 10 is where a second writer appears and this stops being true.

There is no `PREPARED` state either: no two-phase commit, because there is
nothing to coordinate with.

---

## How this compares

**PostgreSQL** does not undo at all. An aborted transaction's tuples stay in the
heap with an `xmax` that marks them invisible, and `VACUUM` reclaims them later.
That works because PostgreSQL is MVCC from the ground up — the "undo" is a
visibility rule, not a write. It is the reason a rollback in PostgreSQL is
nearly free and a rollback here is proportional to pages touched, and also the
reason PostgreSQL needs a vacuum process and this does not.

**SQLite** journals the same thing ChenDB does — whole pages, before they change
— but writes them to a separate rollback-journal file and `fsync`s it first. That
one difference is the whole difference: SQLite's journal survives the process, so
SQLite recovers a crashed transaction on the next open. ChenDB's undo log is in
memory, so it does not. Milestone 9 closes exactly that gap.

**MySQL/InnoDB** keeps a logical-ish undo log in a rollback segment inside the
tablespace, durable and reused for MVCC read views. Both of the things ChenDB
lacks here — durability and multiversion reads — are the same structure in
InnoDB.

---

## What the visualizer shows

```
  ┌──────────────────────────────────────────────────────────────┐
  │ [ BEGIN ] [ COMMIT ] [ ROLLBACK ]   1 stmt · 2 writes · 1 held│
  ├──────────────────────────────────────────────────────────────┤
  │ try it:  [ break it half-way ]  [ create a table, roll back ] │
  ├──────────────────────────────┬───────────────────────────────┤
  │ UNDO LOG                     │ TIMELINE                      │
  │ #1 ↩first  page 4  4 KiB heap│ #1 committed implicit 1 stmt   │
  │                              │ #2 aborted   explicit 1 restored│
  └──────────────────────────────┴───────────────────────────────┘
```

Three things it is built to make visible:

**The undo log is pages, not rows.** The summary reads "2 writes cost 1
before-image — a page is captured once, however many times it changes", computed
from the two counters rather than asserted.

**Most transactions are implicit.** Every row in the timeline is labelled. This
is the most surprising consequence of the milestone and it is invisible without
the label.

**A failure is the demonstration working.** "Break it half-way" runs a real
multi-row `INSERT` whose last row violates `NOT NULL`; the first rows genuinely
are written — the undo log grows while you watch — and then taken back. The
rejection renders as a compact `rejected: …` line, not a red "something went
wrong" banner, because the banner would read as the explorer breaking.

The button is **disabled with an explanation** if the table has no `NOT NULL`
column to violate. `PRIMARY KEY` here is metadata, not a unique index, so the
more familiar duplicate-key version of this demo would silently succeed — and a
demonstration that silently succeeds is worse than one that is not offered.

---

## Try it

```bash
python examples/milestone8_transactions.py
```

```bash
python -m pytest tests/recovery -v
```

---

## What Milestone 9 needs from this

- **The undo log is the thing to make durable.** Milestone 9 writes the same
  before-images to a log file *before* the page changes, which is the entire
  content of "write-ahead".
- **A commit record is what makes commit mean something.** Today commit is a
  state change; with a WAL it becomes one small durable write, and a crash can
  tell a complete transaction from a partial one.
- **Steal stops being a liability.** With a log on disk, the pool may evict an
  uncommitted page freely — recovery undoes it. The policy does not change; the
  log is what makes it safe.
- **`_extend_file_to` becomes redundant.** A WAL that records the allocation can
  repair a short file on recovery, rather than the pager having to prevent one.
