# Milestone 9 — the write-ahead log

Milestone 8 made writes atomic against **errors**. This makes them atomic
against **power loss**, and the entire difference is one small durable write:

```
  BEGIN
    INSERT …          page 4 changes   → log it
    INSERT …          page 7 changes   → log it
  COMMIT              ─────────────────→ log it, and fsync THAT
                                          ↑
                             the pages are still in memory.
                             They do not need to be anywhere else.
```

Kill the process at that point and the pages are gone — every one of them was
still in the buffer pool. Reopen the file and the rows are there, because
recovery replays the log. That is the milestone in one paragraph, and
`tests/recovery/test_a_committed_transaction_survives_without_a_sync` is it as
an assertion.

The test that used to say the opposite — `test_a_crash_mid_transaction_is_not_atomic`
— predicted its own inversion in Milestone 8, and has now been inverted.

---

## What was built

```
engine/wal/
  record.py    one entry: type, LSN, page id, before- and after-images
  log.py       the file: append, flush, scan, checkpoint, truncate
  recovery.py  analysis → redo → undo, on open
```

Plus a hook on the buffer pool's eviction path, `checkpoint_lsn` in the meta
page (format version 4), an LSN stamped into every page as it is written, and
four endpoints — one of which deliberately breaks the database.

---

## The two rules

Everything here follows from two sentences.

**1. A page may not reach the database file before the record describing it is
durable.** This is what "write-ahead" means, and it is one line on the pool's
write-through path:

```python
def _write_to_disk_write_ahead(self, page_id, raw):
    self._wal.flush_through(read_lsn(raw))    # ← the rule
    self._write_to_disk(page_id, raw)
```

Break it and a crash leaves a change on the pages that the log has no record of
— which recovery cannot undo, because it cannot see it.

**2. A commit is not a commit until its record is durable.** One `fsync`, on the
log, at commit time.

From rule 2 comes **no-force**: a commit does not have to flush the pages,
because the log can reconstruct them. From rule 1 comes permission to keep
**stealing**: the pool may evict a dirty uncommitted page, because recovery can
put it back. Steal + no-force is the fastest pair and the pair that needs a log.
ChenDB has had steal since Milestone 7 and gets no-force here — which is why the
buffer pool did not have to change to become crash-safe.

---

## An LSN is a byte offset

Not a counter. That is what PostgreSQL does, and it makes two otherwise-fiddly
things disappear:

- **"durable up to LSN *n*"** becomes "the first *n* bytes are on disk" — a
  comparison, not a lookup;
- **a record's LSN is knowable before it is written**, which turns out to be
  load-bearing.

That second one is a genuine chicken-and-egg. A page has to *carry* the LSN of
the record describing it, and the record has to *contain* the page. Neither can
be built first:

```python
lsn = log.next_lsn          # what the record will be, before it exists
raw = stamp_lsn(raw, lsn)   # the page now knows its record
log.append(after=raw)       # the record now contains the page
```

Get it wrong and the after-image in the log carries an LSN of zero, so redo
writes a page that still looks un-redone, and every subsequent recovery redoes
it again forever.

### The base, and why the meta page had to change

A checkpoint truncates the log file, so byte 0 of the *file* stops being byte 0
of the *stream*. `checkpoint_lsn` in the meta page is the difference, and it is
the only field format version 4 added.

Without it, LSNs would restart at zero after every checkpoint, and a record
written after one would compare *below* the LSN already stamped on a page —
so redo would look at it, decide the page was ahead, and skip work it had to do.
`test_work_after_a_checkpoint_still_survives_a_crash` is the assertion.

---

## Recovery: three passes, and the middle one looks wrong

```
  log:  [u1 t7] [u2 t7] [u3 t9] [commit t7] [u4 t9] ✗ crash

  analysis   t7 committed → winner
             t9 never did → loser
  redo       replay u1 u2 u3 u4 — everything, losers included
  undo       walk t9's records back, restore before-images,
             logging each restore, then abort t9
```

**Redo replays the losers too.** ARIES calls this *repeating history*, and the
reason is that recovery cannot know which of a loser's changes reached the disk.
Rather than reason about it per page, it puts the database into exactly the
state the crash left it in — every logged change applied — and then rolls the
losers back from there, using the same undo path a live rollback uses. One
mechanism, exercised constantly, instead of a second one that only runs after a
crash and is therefore never tested.

### `ABORT` counts as finishing

A rolled-back transaction wrote its restores through the ordinary page path, so
they are already in the log as `UPDATE` records. Replaying them lands on the
pre-transaction state, which is where the rollback already was. Undoing it again
would be undoing the undo.

### Undo is logged

Each restore is appended as an `UPDATE` record *before* it is applied. ARIES
calls these compensation log records, and the point is that a machine which
crashed once can crash again while recovering: the completed part of an
interrupted undo is in the log, so the next recovery redoes it in its own redo
pass and only undoes what is left.

### A bug the dictionary test found

Recovery is tested against a `dict[int, bytes]`, because the whole point of the
callback boundary is that `recover` never learns what a page is. That test found
this:

```python
def _page_lsn_on_disk(self, page_id) -> int:
    if <no page there>:
        return -1          # was 0
```

**Zero is a real LSN** — it is the first record ever written, which for a
brand-new database is the meta page. A crash before that page reaches the disk
leaves nothing to read, and answering `0` had redo compare `0 >= 0`, decide the
page was current, and skip restoring the only page that makes the file a
database. `test_a_database_created_and_killed_immediately_is_recoverable`
reproduces it as a real `SIGKILL`.

### What is *not* here

No dirty-page table and no transaction table are reconstructed during analysis.
Both exist in real ARIES so redo can start at the earliest change that might not
be on disk rather than at the checkpoint. ChenDB's checkpoints are **sharp** —
they stop the world and flush everything — so the earliest such change is always
the first record after the checkpoint. That simplification is why
`recovery.py` is one page long instead of five, and it is also why it would not
survive contact with a hundred-gigabyte buffer pool. Real systems use *fuzzy*
checkpoints for exactly that reason.

---

## What it costs

### Writing

```
  insert, no log      14.4 us
  insert, logged      18.7 us      +30%
```

Every page write now also encodes a record, checksums it, and stamps an LSN into
the page — which means a second CRC32 pass over the page, because the LSN lives
inside the range the page checksum covers.

### The commit fsync

```
  commit, fsync on      88.7 us     ->  11,300 commits/s
  commit, fsync off     29.2 us     ->  34,300 commits/s
  the fsync itself      59.5 us
```

That ceiling has **nothing to do with how much work each transaction did**. It
is the disk, once per commit, and it is why real systems invented **group
commit**: several concurrent committers share one flush. ChenDB has one writer,
so there is nobody to share with, and `set_sync_policy` exists only so the
benchmark can price the fsync — not as a durability setting.

SQLite draws the same line at `synchronous=NORMAL` versus `FULL`: NORMAL skips
the per-commit fsync and is durable against a process crash but not a machine
crash.

### The log volume, and the one optimisation

The first working version logged a whole page image per write. Measured:

```
  20,000 rows in one transaction

    log            81.1 MiB
    pages           420 KiB
    amplification   197x        ← unshippable
```

Whole-page logging is what makes the log ignorant of heap rows, B+ tree nodes and
catalog tuples — the same trade Milestone 8's undo log made — but 197× is not a
trade, it is a defect.

The fix is small and rests on a fact about *staged* records: writing the same
page twice in a row produces two records of which only the second matters, since
redo replays them in order and the first is immediately overwritten. So if the
previous **unflushed** record is an update to the same page by the same
transaction, the new one replaces it:

```
  20,000 rows in one transaction, with coalescing

    log             2.10 MiB     ← 39x smaller
    amplification    5.1x
    records            516        (22,000 coalesced away — 98%)
    insert cost      18.7 us      (was 25.8 us)
```

It is safe only while the record is staged. Once it has been flushed, a page
carrying its LSN may already be on the disk, and rewriting the record behind
that page would leave the two disagreeing — and the write-ahead rule guarantees
they cannot overlap, because a page reaching the disk forces a flush first,
which empties the buffer.

What this does **not** fix is amplification across flush boundaries. A
transaction spread over many statements still writes one page image per
statement. Fixing that properly means logging **deltas** — "insert this tuple
into page 7", a few dozen bytes — which is what real systems do, at the price of
a redo routine per operation that has to be exactly the inverse of the operation
itself. PostgreSQL splits the difference: a full-page image the first time a page
changes after a checkpoint, protecting against torn writes, and deltas after.

### Recovery

```
  1,000 rows:   113 KiB log,  22 records ->  0.6 ms
 10,000 rows:  1.1 MiB log, 222 records ->  2.2 ms
```

Linear in log size, which is what checkpoint frequency is for.

---

## Checkpoints, and an ordering that is not the obvious one

```
  1. flush dirty pages, fsync the database file
  2. append CHECKPOINT, fsync the log
  3. write the meta page's new checkpoint_lsn straight to the file
  4. truncate the log
```

Step 3 is the only place in the engine that writes a page around both the log
and the pool, and it has to be: logging it would put a record into the log this
call is about to truncate, and the new `checkpoint_lsn` is not known until step 2
has decided where the log restarts.

**A crash between 3 and 4** is the interesting case. The meta page says the log
restarts at the new LSN, but the file still holds the old records at the old one.
The next open reads them at the wrong position, each record's stored LSN fails to
match where it was found, and the log correctly reads as empty — which it is, in
the sense that matters, because step 1 already put every page those records
describe onto the disk. That position check in `decode_record` exists for exactly
this, and `test_a_record_found_at_the_wrong_offset_is_rejected` is why it stays.

A checkpoint is **refused while a transaction is open**: discarding the log would
discard that transaction's before-images. Real systems keep the log back to the
oldest active transaction's first record instead of truncating wholesale;
refusing is the version of that rule an all-or-nothing checkpoint can express.

---

## What Milestone 8 promised and this delivered

The Milestone 8 doc listed four things this milestone would need. All four
landed:

- **The undo log became durable.** The same before-images, under the same
  first-write-wins rule, now go to the log as well as memory.
- **`MAX_UNDO_BYTES` stopped being a correctness limit.** A transaction that
  overflows the in-memory cache no longer fails — it stops caching, and
  `Database.rollback` reads what it needs from the WAL.
- **Steal stopped being a liability**, without the pool changing at all.
- **`_extend_file_to` became redundant.** Milestone 8 pre-extended the file with
  an `ftruncate` per allocation because the pool could evict the meta page before
  the pages it references. Recovery repairs that now, so the file-length check
  moved to *after* recovery and the syscall is gone.

That last one needed one more fix than expected. `before_write` marks a page
captured whether or not there is room to keep its bytes — because first-write-wins
is a *decision*, and the WAL asks the same question through the same method. When
the cap made the decision lapse, the log started recording a fresh "before" on
every later write to the page: mid-transaction states, each claiming to be the
state to roll back to.

---

## How this compares

**SQLite** in WAL mode is the closest relative and does the opposite thing. Its
log holds *after-images only* and the database file is not written until a
checkpoint, so readers consult a WAL index to find the newest version of a page —
no undo pass at recovery, at the price of a lookup on every read. ChenDB writes
through to the file and logs both images, which keeps the read path untouched.
Both files are named `-wal` for the same reason: adjacent in a listing, obviously
derived.

**PostgreSQL** logs physiologically — the operation, not the page — with a
full-page image the first time each page changes after a checkpoint. That is
strictly better than what is here and needs a redo routine per operation type.
Its LSN is a byte position in the WAL stream, which is where that idea came from.
Its recovery does not undo: aborted transactions are handled by MVCC visibility,
so there is no undo pass at all.

**MySQL/InnoDB** keeps redo in a ring of log files and undo in rollback segments
inside the tablespace, reused for MVCC read views. Both of the things ChenDB
still lacks — physiological redo and multiversion reads — are the same structure
in InnoDB.

---

## What the visualizer shows

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ 275 KiB · 66 records · fsync 50 µs · 20,132/s  [Checkpoint][Crash]│
  ├──────────────────────────────────────────────────────────────────┤
  │ LAST RECOVERY                                                     │
  │  analysis  66 scanned   finished #1 #2 #3 · interrupted #4  27 µs │
  │  redo      0 replayed   63 already current                 306 µs │
  │  undo      1 put back   restored from their before-images  126 µs │
  ├──────────────────────────────────────────────────────────────────┤
  │ LSN    Txn  Type      Page  Size   Undo   ↩ prev                  │
  │ 0      —    update    0     556 B  —      —                       │
  │ 556    #1   update    4     1 KiB  512 B  —                       │
  │ 1668   #1   commit    —     44 B   —      556                     │
  └──────────────────────────────────────────────────────────────────┘
```

**The crash button is why this workspace exists.** Every other panel shows what
the engine is doing; this one lets you break it and watch it put itself back. A
durability claim is not believable from a description, and there is no honest way
to demonstrate recovery through a clean shutdown.

It destroys uncommitted work on purpose, behind a confirm, and reports the row
count on both sides so the reader can check rather than trust:

```
  users: 53 → 3     50 uncommitted row(s) rolled back
```

Three other things it is built to show:

- **The fsync as a commit ceiling.** One second divided by the measured average
  is a number with nothing in it about how much work each transaction did.
- **`prev_lsn` drawn as a column.** It is the part of an ARIES record people have
  read about and never seen: a transaction is a chain threaded through a file
  that is otherwise strictly chronological.
- **Skipped records given equal billing with replayed ones.** A record skipped
  because the page already had it is the last checkpoint paying for itself, and a
  panel counting only work done would make checkpoints look pointless.

---

## Try it

```bash
python examples/milestone9_wal.py
```

```bash
python -m pytest tests/recovery tests/unit/test_wal.py -v
```

---

## What Milestone 10 needs from this

- **Concurrency makes the fsync worth amortising.** Group commit is meaningless
  with one writer and is the obvious first optimisation with several.
- **The log is where MVCC's old versions would live.** InnoDB reuses its undo
  records to serve read views; the before-images here are already the right data
  in the wrong place — in memory, and discarded at commit.
- **`prev_lsn` starts earning its keep.** With many interleaved transactions,
  walking one transaction's records by chain rather than rescanning the log stops
  being a nicety.
- **Page-granularity is the wall.** Two transactions writing different rows on
  the same page conflict at page level today, which is invisible with one writer
  and is the first thing a second writer hits.
