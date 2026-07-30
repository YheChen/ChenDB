# Milestone 5: B+ tree indexes

A sequential scan of a 20,000-row table reads 233 pages to find one row. With an
index it reads 4. That single change (O(pages) to O(log pages)) is what
separates a database from a file of rows, and it is what this milestone builds.

It also builds the reason *not* to use one, because a rule-based planner gets
that wrong and the numbers below show it doing so.

```
engine/index/
    key.py         order-preserving key encoding, read this first
    node.py        one slotted page interpreted as a tree node
    bplustree.py   search, insert, split, range scan, delete
```

---

## What was built

| | |
|---|---|
| **Storage** | `BTREE_INTERNAL` / `BTREE_LEAF` pages, `Page.insert_at` / `delete_at` |
| **Index** | order-preserving keys, disk-backed B+ tree, linked leaves, unique constraint |
| **Catalog** | `chendb_indexes` (system table id 3), format version 3 |
| **SQL** | `CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON table (column)` |
| **Executor** | `IndexScan`, rule-based access-path choice, index maintenance on insert/delete |
| **Diagnostics** | 5 index events, `StepKind.INDEX_OPERATION`, `ResumeMode.UNTIL_INDEX_OPERATION` |
| **API** | `GET/POST /indexes`, `GET /indexes/{name}`, `GET /indexes/{name}/search` |
| **Visualizer** | Index workspace: real tree, traced descent, node → page inspector |

---

## The design crux: keys that sort as bytes

Milestone 1 chose little-endian for record encoding, because it matches every
CPU and makes writing an integer a memory copy. That choice is wrong for index
keys, and the tree cannot work around it:

```
 value   little-endian bytes
     1   01 00 00 00 00 00 00 00
   256   00 01 00 00 00 00 00 00
```

Compared byte by byte, `1 > 256`, because the first byte examined is the *least*
significant one. Two's complement makes it worse, `-1` is all ones, which is the
largest unsigned value there is.

There were two ways out.

**Decode both sides and compare Python values.** No encoding module at all. But
a descent through a depth-3 tree with 200 entries per node does ~24
comparisons, each costing two `struct.unpack` calls plus a Python `<`.

**Encode so that byte order is value order.** A comparison becomes one `bytes`
comparison, which CPython dispatches to `memcmp` in C. The key is then opaque:
nothing in `bplustree.py` branches on the column's type.

ChenDB takes the second, which is what RocksDB, FoundationDB and LevelDB do, and
why their APIs are byte-string-oriented. The cost is `engine/index/key.py`: one
transform per type, each of which has to be exactly right, and each with a test
that sorts a list of values and asserts the encodings sort the same way.

The transforms, and the traps in each:

- **INTEGER**: big-endian of `value + 2**63`. The bias flips the sign bit,
  mapping `INT64_MIN → 0x0000…` and `INT64_MAX → 0xffff…`, a monotonic map from
  signed to unsigned.
- **FLOAT**: big-endian IEEE-754 bits, then flip *all* bits if negative and
  *only the sign bit* if positive. IEEE-754 was designed so positive floats sort
  correctly as integers; the transform extends that to negatives, whose
  magnitude ordering runs backwards. `-0.0` is normalised to `0.0`, or a unique
  index would accept both. `NaN` sorts above everything, as in PostgreSQL.
- **TEXT**: raw UTF-8, *no length prefix*. UTF-8's defining property is that
  byte order equals code-point order. A length prefix would sort `"z"` before
  `"aa"`, which is the classic version of this mistake.

Ordering is binary, not linguistic: `"Z" < "a"`. Real collation is a much larger
problem, PostgreSQL delegates it to ICU or libc, and a glibc upgrade that
changed collation famously corrupted people's indexes silently. Binary ordering
at least never changes underneath you.

### NULL and minus infinity

Every key carries a one-byte tag. It does two jobs at once:

```
0x00  minus infinity   the first separator of every internal node
0x01  NULL             sorts below every value
0x02  a value          payload follows
```

The sentinel removes the need for a special "leftmost child" field in the page
header: slot 0 of an internal node is always minus infinity, so a descent always
finds a child to follow. PostgreSQL does the same thing. "the first data key on
a non-leaf page is always minus infinity".

The `NULL` tag is load-bearing in a way that is easy to miss. `WHERE age < 18`
becomes a range scan bounded only from above, and an unbounded low end would
sweep up every NULL key, but no comparison is ever true for NULL. The planner
therefore anchors the low end at `SMALLEST_VALUE_KEY` (`0x02`), which excludes
the NULL tag by construction.

---

## The tree

```
                      ┌──────────────────────────┐
   internal           │  -∞ │ 40 │ 80            │   root
   (separators only)  └───┬──┴──┬─┴──┬───────────┘
                ┌─────────┘     │    └─────────┐
                ▼               ▼              ▼
          ┌──────────┐    ┌──────────┐   ┌──────────┐
   leaves │ 10 20 30 │───▶│ 40 55 70 │──▶│ 80 90 99 │──▶ ∅
          └──────────┘    └──────────┘   └──────────┘
           each entry: key ‖ record id → a row in the heap
```

Three properties do all the work.

**Every value lives in a leaf.** Internal nodes are pure routing, so they stay
small and the fanout stays large. A plain B-tree stores values in internal nodes
too, so it is taller for the same data.

**Leaves are linked.** A range scan descends once and then walks sideways, so
`WHERE age BETWEEN 30 AND 40` costs one descent plus the leaves it touches, no
re-descent per row.

**Growth is at the root.** A node that overflows splits and pushes a separator
up; when the root splits, a new root is allocated *above* it. The old root keeps
its page id and becomes the leftmost child, so nothing has to be rewritten, and
every leaf stays at the same depth without a rebalancing pass.

### Duplicates, and why the record id is in the key

Entries sort by `(key, record_id)`. Every entry in the tree is therefore unique,
which buys three things a per-key list of record ids does not:

- deleting one row's entry is a descent, not a scan of every duplicate;
- a split can happen anywhere, including in the middle of a run of duplicates;
- a page holding one repeated key is still splittable, a list is not, once it
  outgrows a page.

PostgreSQL adopted exactly this in version 12 ("make the heap TID a tiebreaker
column") and reported large reductions in index bloat on low-cardinality
columns.

The two compare as a *pair*, not as concatenated bytes. Concatenating would
reintroduce the prefix problem the key encoding avoids: `"ab" ‖ rid` and
`"abc" ‖ rid` interleave, and whether the comparison comes out right would
depend on the numeric value of the record id. Splitting the fixed-width 6-byte
suffix off by length is exact and needs no escaping layer, which is also the
reason Milestone 5 indexes one column and not two. A composite key *would* need
that layer: escape `0x00` as `0x00 0xff`, terminate with `0x00 0x00`, exactly as
FoundationDB's tuple layer does.

### Splitting by bytes, not by count

`plan_split` balances the two halves by **byte total**, then clamps the result to
the range where both halves actually fit. Balancing by entry count breaks as soon
as entries vary in width: a TEXT index holding `"a"` and a 200-character string
would cut 50/50 by count, leave one page nearly full, and split again on the very
next insert.

The clamp range is never empty, and it is worth seeing why: a node only overflows
once, so the total is at most one page (what already fitted) plus one entry (at
most one page), and any single entry fits on a page by itself.

---

## What this implementation does not do

**No merging on delete.** An entry is removed from its leaf; the leaf is left
underfull, and an emptied leaf stays in the tree rather than being unlinked. This
is a choice, not an omission. Merging requires locking a node's sibling *and* its
parent while a concurrent descent may be passing through, and getting it wrong
corrupts the tree in ways that only appear under load. PostgreSQL took until
version 11 to reclaim empty index pages and still never merges partly-full ones;
SQLite does merge, and pays with a far more complex balance routine.

The cost is measurable and bounded: a delete-heavy index keeps its pages. Space
is reused by later inserts into the same key range (`test_space_from_deletions_is_reused_in_place`
pins that down) but never returned to the file.

**No bulk loading.** `CREATE INDEX` inserts row by row: O(n log n), with a split
roughly every half-node. Over 20,000 rows that is 3.2 seconds and 185 splits. A
real system sorts the keys first and packs leaves to capacity in one pass, which
is O(n log n) for the sort and then O(n), and produces a tree with no wasted
space.

**No concurrency.** One writer at a time, enforced by the database-level lock.
Real B+ trees use latch coupling (grab the child's latch, release the parent's)
and the B<sup>link</sup>-tree right-link trick that lets a reader recover when a
concurrent split moves the key it wanted. Milestone 10.

---

## Choosing an access path

There are now two ways to read a table, so the planner makes its first real
decision:

```
WHERE age = 30                index on age?  →  IndexScan, key = 30
WHERE age >= 20 AND age < 30  index on age?  →  IndexScan, key >= 20 AND key < 30
WHERE age = 7 AND name = 'x'  index on age?  →  IndexScan  +  Filter(name = 'x')
WHERE name LIKE 'a%'          no index       →  SeqScan + Filter
WHERE age <> 7                index on age   →  SeqScan + Filter
```

The rule is "use an index whenever one covers a comparison". A conjunction is
flattened, comparisons on one indexed column are folded into a single
`[low, high]` range, and whatever the index could not express stays as a
residual `Filter`, which is exactly PostgreSQL's "Index Cond" versus "Filter"
distinction, and the distinction matters: an index condition bounds how much is
*read*, a filter only discards what was already read.

`<>` is refused deliberately: an index cannot bound it, so the scan would read
the whole tree and then do a random heap read per row.

### Where the rule is wrong

The rule never asks *how many rows will match*, and that is the question that
decides whether an index helps. Measured on 20,000 rows, 4 KiB pages
(`benchmarks/index_vs_scan.py`):

```
 predicate            rows    no index      index    pages (scan → index)
 ─────────────────  ──────  ──────────  ─────────    ────────────────────
 id = k (point)          1    60.0 ms    0.183 ms    233 →     4
 bucket < 1             20    57.9 ms      0.7 ms    233 →    22
 bucket < 10           200    58.6 ms      4.8 ms
 bucket < 50          1000    60.0 ms     22.7 ms    233 →  1009
 bucket < 200         4000    63.3 ms     92.9 ms    ← index now slower
 bucket < 700        10000    59.8 ms    225.2 ms    166 → 10076
```

A point lookup is **328× faster**. A predicate matching 70% of the table is
**3.8× slower**, and the page counts say why: a sequential scan reads every page
exactly once whatever the predicate, while an index scan reads one heap page per
matching row. The same page over and over when several matches share it, since
there is no buffer pool until Milestone 7.

The crossover sits between 5% and 20% selectivity here. Estimating selectivity,
and therefore choosing correctly, is Milestone 6. Milestone 5 gets it visibly
wrong, which is a better teaching artifact than getting it invisibly right.

---

## Index maintenance

An index that goes stale is worse than no index, so every write path updates
every index on the table:

- `insert_many` descends each index once per row. An insert into a table with
  three indexes is four B+ tree operations plus the heap write. That is the cost
  indexes impose on writes, and the reason you do not add one per column.
- `delete` has to *read the row first*. An index entry is keyed on the value, so
  removing it needs to know what the value was. This is why PostgreSQL instead
  leaves index entries pointing at dead tuples and cleans them up in `VACUUM`.

None of it is atomic. A unique violation on the second index leaves the first
updated and the row in the heap. One more thing Milestone 9's write-ahead log is
for.

`IndexScan` skips a record id whose row is gone rather than failing. The same
recovery PostgreSQL performs when it reaches a dead tuple through an index.

---

## Stepping through an index

`ResumeMode.UNTIL_INDEX_OPERATION` was two lines:

```python
_STOPS_AT = {
    ...
    ResumeMode.UNTIL_INDEX_OPERATION: frozenset({StepKind.INDEX_OPERATION}),
}
_INDEX_EVENTS = (IndexSearchEvent, NodeSplitEvent, RangeScanEvent)
```

The controller is registered as a diagnostics sink, so a B+ tree event *becomes*
a checkpoint without the tree knowing that stepping exists. The same mechanism
`UNTIL_PAGE_READ` has used since Milestone 3, and the payoff for Milestone 1
building a general event bus instead of a logging call.

---

## The visualizer

A new **Indexes** workspace, gated on `features.indexes` like every other tab.

The tree is drawn by hand in SVG rather than with a tree component, because a
generic one gets two things wrong that are the whole point:

- **a node holds many keys, not one.** A binary-tree renderer draws a circle with
  a label; a B+ tree node is a row of cells whose width is how full it is, and a
  node filling up is what precedes a split;
- **leaves are linked, and that link is not a tree edge.** It runs sideways
  between siblings that may have different parents, so it is drawn dashed.

d3-hierarchy was considered and rejected for the same reason: its tidy-tree
assumes uniform node widths and only draws tree edges, so bending it to this
shape is more code than placing the boxes directly. That keeps the dependency
list where it is.

Real indexes are *wide* (600 rows on a 512-byte page is thirty leaves) so the
cell budget per node shrinks as a level gets wider, eliding the middle and
keeping the first and last keys, which are the two that bound the subtree. The
count of hidden keys is shown on the node's label, so nothing is silently lost.

Typing a key into **Point lookup** runs a real search and highlights the path the
engine actually took, scrolling the leaf into view:

```
found  yes    matches  13    pages read  4    height  3
path: p82 → p81 → p67
```

Clicking any node opens it in the page inspector, where the same bytes appear as
a `BTREE_LEAF` page with a checksum, a slot directory and a hexdump. There is no
separate model of the tree in the browser to drift out of sync with the one on
disk, which was the requirement.

---

## Try it

```bash
python examples/milestone5_indexes.py
```

```bash
python benchmarks/index_vs_scan.py
```

```sql
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, age INTEGER);
INSERT INTO users VALUES (1, 'ada@example.com', 36);
CREATE UNIQUE INDEX users_email ON users (email);
CREATE INDEX users_age ON users (age);

SELECT id FROM users WHERE age >= 30 AND age < 40;   -- IndexScan
SELECT id FROM users WHERE email <> 'x';             -- SeqScan; <> cannot be bounded
```

---

## What Milestone 6 needs from this

- **Selectivity estimates.** The numbers above are the shape of the problem; a
  cost model needs statistics (row counts, distinct values, histograms) to
  predict them.
- **`EXPLAIN`.** `AccessPath` already separates the index condition from the
  residual filter, which is the interesting half of what `EXPLAIN` prints.
- **Ordered output.** `IndexScan` emits rows in key order for free. Nothing
  exploits it because there is no `ORDER BY` yet; when there is, an indexed sort
  becomes a no-op.
