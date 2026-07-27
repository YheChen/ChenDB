# On-disk format (version 1)

A ChenDB database is one file. The file is an array of fixed-size pages, and
nothing else. Page *n* lives at byte offset `n × page_size` — that single
property is why essentially every disk-based database uses fixed-size pages,
and it is what makes the disk map in the visualizer possible.

```
      0        page_size      2·page_size     3·page_size
      ├────────────┼────────────┼────────────┼─────────── …
      │  page 0    │  page 1    │  page 2    │  page 3
      │  META      │  SCHEMA    │  SCHEMA    │  HEAP
      └────────────┴────────────┴────────────┴───────────
```

`page_size` is chosen at creation and is immutable thereafter. The default is
4096: it matches SQLite's default and the block size of most filesystems, so
one page read is one filesystem block. PostgreSQL uses 8192 instead — more read
amplification on a point lookup, but a shorter page chain and a larger maximum
tuple. The test suite uses 256-byte pages so that four rows fill a page and
chaining is exercised by a handful of inserts.

---

## Page 0 — the meta page

Page 0 has its own layout rather than the generic page header, so the magic
string lands at file offset 0 and `head -c 16 x.chendb` identifies the file.
SQLite does the same with its 100-byte header at the start of page 1;
PostgreSQL instead keeps cluster metadata in a separate `pg_control` file.

```
 off  size  field                   notes
 ───  ────  ──────────────────────  ────────────────────────────────────
   0    16  magic                   b"ChenDB Format 1\x00"
  16     4  format_version          u32 — bumped on any layout change
  20     4  page_size               u32 — fixed at creation
  24     4  page_count              u32 — pages allocated, this one included
  28     4  free_list_head          u32 — head of the recycled-page chain
  32     4  catalog_tables_first    u32 ┐ chendb_tables heap
  36     4  catalog_tables_last     u32 ┘ …and its tail, so append is O(1)
  40     4  catalog_columns_first   u32 ┐ chendb_columns heap
  44     4  catalog_columns_last    u32 ┘
  48     4  catalog_indexes_first   u32 ┐ chendb_indexes heap      (v3)
  52     4  catalog_indexes_last    u32 ┘
  56     4  next_object_id          u32 — shared table/index counter (v3)
  60     8  lsn                     u64 — last log record for this page (v4)
  68     8  checkpoint_lsn          u64 — LSN of the log file's byte 0   (v4)
  76     4  next_xid                u32 — next transaction id            (v5)
  80     4  flags                   u32 — reserved
  84     4  checksum                u32 — CRC32 over bytes [0, 84)
 ─────────────────────────────────────────────────────────────────────────
  88 bytes; the rest of the page is reserved and zero-filled.
```

`next_xid` becomes the *frozen horizon* on open: every id below it belonged to a
transaction that has finished, and because a rollback here physically removes
its work, every row still on disk came from one that **committed**. That is
ChenDB's entire commit log, in one number — PostgreSQL needs `pg_xact` and a
freeze job instead, because it does not undo.

`checkpoint_lsn` is here rather than in the log because it has to be readable
*before* the log is opened: it says what LSN the log file's first byte
corresponds to. A checkpoint truncates the log, so that is not zero after the
first one — and without it, LSNs would restart, page LSNs written earlier would
compare greater than records written later, and redo would skip work it had to
do.

`0xFFFFFFFF` is the null page pointer. Zero cannot serve as the sentinel
because page 0 is a real page.

Only these six pointers are needed to *start* reading. Everything else — where
each table's heap begins, where each index's tree is rooted — is a row in a
system table. That is what the catalog buys: creating a table or an index is an
insert, not a file-format change. Milestone 5 still had to bump the version,
but only because it added a new *system* table, which by definition cannot
bootstrap itself.

`catalog_tables_last` exists purely for speed: without it, appending a row means
walking the whole chain, making a bulk load O(n²) in pages. With it, insert is
O(1).

### Version history

| version | milestone | what changed |
|---|---|---|
| 1 | 1 | One table per file: `heap_first_page`, `heap_last_page`, `schema_page_id` |
| 2 | 4 | Those three replaced by the two catalog heaps plus `next_table_id` |
| 3 | 5 | `catalog_indexes_*` added; `next_table_id` → `next_object_id` |
| 4 | 9 | `checkpoint_lsn` added; `lsn` stops being reserved and starts being written |
| 5 | 10 | `next_xid` added; every record gains an 8-byte tuple header |

There is no in-place upgrade. Every bump so far moved where the catalog lives,
so reading an old file would mean carrying a reader per layout — worth it for a
shipped database, not for a teaching one. An old file is rejected with a message
naming the milestone that changed it.

---

## Slotted pages

Every page other than page 0 is a *slotted page*: a fixed-size block split into
two regions that grow toward each other.

```
 0                                                            page_size
 ├────────────┬──────────────────┬───────────────┬────────────────────┤
 │   header   │  slot directory  │  free space   │    record data     │
 │    24 B    │   4 B per slot   │               │                    │
 └────────────┴──────────────────┴───────────────┴────────────────────┘
              ↑                  ↑               ↑
             24            free_start        free_end          page_size
                           (grows →)         (← grows)
```

### Page header — 24 bytes

```
 off  size  field          notes
 ───  ────  ─────────────  ─────────────────────────────────────────────
   0     4  checksum       u32 — CRC32 over bytes [4, page_size)
   4     8  lsn            u64 — the log record that last changed this page
  12     1  page_type      u8  — 0 FREE · 1 META · 2 HEAP · 3 SCHEMA
                                 4/5 BTREE_* (reserved) · 6 OVERFLOW
  13     1  flags          u8  — reserved
  14     2  slot_count     u16 — directory entries, tombstones included
  16     2  free_start     u16 — end of the directory   (pd_lower)
  18     2  free_end       u16 — start of record data   (pd_upper)
  20     4  next_page_id   u32 — next page in this heap's chain
```

`free_start` is redundant — it always equals `24 + 4 × slot_count` — but it is
stored anyway, mirroring PostgreSQL's `pd_lower`/`pd_upper`, and
`Page.validate()` asserts the invariant on every read. A redundant field that
is checked is a corruption detector.

The `lsn` was reserved from Milestone 1 and became load-bearing in Milestone 9.
Recovery compares it against each log record: a record whose LSN the page has
already passed is skipped, which is what makes replaying a log idempotent and
therefore what makes a crash *during recovery* survivable. It sits inside the
checksum's range, so stamping it means recomputing the checksum — which is why
`stamp_lsn` exists and why a logged write pays a second CRC32 pass.

### Slot directory — 4 bytes per entry

```
 slot i, at offset 24 + 4i:
     off  size  field
       0     2  offset   u16 — where the record starts, or 0 if dead
       2     2  length   u16 — record length in bytes
```

A tombstone is `(0, 0)`. Offset 0 is unambiguous because a live record always
starts after the header.

PostgreSQL's equivalent `ItemIdData` is also 4 bytes but bit-packs
`lp_off:15, lp_flags:2, lp_len:15`, buying two spare flag bits. SQLite's cell
pointer array stores only offsets and re-derives lengths by parsing the cell —
2 bytes cheaper per row, at the cost of CPU on every access.

### Why the indirection

Callers hold a *slot index*, never a byte offset. That one level of indirection
is the whole point:

- **Records can move within the page.** Compaction slides live records
  together and rewrites their slot offsets. Slot indices are unchanged, so
  every `RecordId` held anywhere in the system stays valid. This is exactly why
  Milestone 5's index can store `RecordId`s in its leaves.
- **Records are variable length, yet lookup is O(1)** — one array index.
- **Deletion is O(1)** — write `(0, 0)`.

### Growth

```
 empty page                     after two inserts
 ┌──────────┬─────────────┐     ┌──────┬────┬───────┬──────┬──────┐
 │  header  │    free     │     │header│slots│ free │ rec1 │ rec0 │
 └──────────┴─────────────┘     └──────┴────┴───────┴──────┴──────┘
 24        24          4096     24    32     free_end        4096
```

Slot 0 sits highest in the page; each later record is placed below it. An
insert of `n` bytes consumes `n + 4` (record plus slot entry), or just `n` when
a tombstoned slot can be reused.

Largest storable record: `page_size − 24 − 4`, i.e. **4068 bytes** on a 4 KiB
page. Anything larger raises. Real systems store oversized values out of line —
PostgreSQL's TOAST tables, SQLite's overflow-page chains. ChenDB does not yet;
`PageType.OVERFLOW` is reserved for it.

### Compaction

Deleting a record writes a tombstone; the bytes stay put. `reclaimable_space`
reports what compaction would recover:

```
 before compact()                    after compact()
 ┌──────┬────┬──────┬────┬────┬────┐ ┌──────┬────┬─────────┬────┬────┐
 │header│slot│ free │ r2 │ ░░ │ r0 │ │header│slot│  free   │ r2 │ r0 │
 └──────┴────┴──────┴────┴────┴────┘ └──────┴────┴─────────┴────┴────┘
                          dead r1                    r1's bytes recovered
```

Slot 1 stays in the directory as a tombstone so slot 2 does not renumber. Only
*trailing* tombstones are trimmed. The heap decides when to compact — the page
only knows how — because "compact this page or move to another" is a storage
policy question. PostgreSQL defers the same work to page pruning and `VACUUM`.

---

## Record format

```
 ┌──────┬──────┬──────────────┬─────────┬─────────┬─────┬─────────┐
 │ xmin │ xmax │ null bitmap  │ value 0 │ value 1 │ ... │ value n │
 │ u32  │ u32  │  ⌈cols/8⌉ B  │         │         │     │         │
 └──────┴──────┴──────────────┴─────────┴─────────┴─────┴─────────┘
```

Bit *i* of the bitmap set means column *i* is NULL, and a NULL contributes
**no bytes** to the value area. A row of five NULLs costs one byte.

### The tuple header — 8 bytes (v5)

`xmin` is the transaction that created this version; `xmax` is the one that
deleted it, or zero. Those eight bytes are what make a row a *version*, and
they are the whole of MVCC's storage cost — 24% on a small row, against
PostgreSQL's 23-byte `HeapTupleHeaderData`.

The header goes **first** so a reader can decide whether a version is visible
before decoding any of it: an invisible row costs one `struct.unpack` rather
than a walk of every column.

A **delete rewrites `xmax` in place** and leaves everything else alone, which is
why `Page.overwrite` exists and why it refuses anything but an equal-length
write. The slot stays live and the row still decodes — a transaction whose
snapshot predates the delete has to be able to read it — and the space comes
back at `VACUUM`.

The catalog's own tables carry **no** tuple header. They do not need versions:
a rolled-back `CREATE TABLE` is undone by restoring the page, so there is never
a catalog row anybody wants an older version of. PostgreSQL does the opposite,
because it has no undo and no other way to take a failed DDL back.

### Value encodings

| Type      | Python  | Bytes | Layout                                  |
|-----------|---------|-------|-----------------------------------------|
| `INTEGER` | `int`   | 8     | `<q` two's-complement little-endian     |
| `FLOAT`   | `float` | 8     | `<d` IEEE-754 binary64                  |
| `BOOLEAN` | `bool`  | 1     | `0x00` / `0x01`                         |
| `TEXT`    | `str`   | 4 + n | `<I` **byte** length, then UTF-8        |

### A worked example

Schema `(id INTEGER PK, email TEXT NOT NULL, age INTEGER, active BOOLEAN)`,
row `(2, "alan@example.com", NULL, False)`:

```
 offset  bytes                              meaning
 ──────  ─────────────────────────────────  ─────────────────────────────
      0  04                                 null bitmap 0b0000_0100
                                            → bit 2 set → age IS NULL
      1  02 00 00 00 00 00 00 00            id = 2
      9  10 00 00 00                        email length = 16 bytes
     13  61 6c 61 6e 40 65 78 61 6d 70 6c   "alan@exampl"
     24  65 2e 63 6f 6d                     "e.com"
     29  00                                 active = false
 ──────
     30 bytes total; age occupies none
```

That is the byte sequence the page inspector's Hex tab highlights when you
select the slot.

### Decisions worth naming

**The bitmap is always present.** PostgreSQL omits it entirely when a tuple has
no NULLs, flagging that in `t_infomask`; on a `NOT NULL` table that saves a
byte per row plus alignment padding. ChenDB always writes it, trading a byte
for a branch-free decoder.

**Values are sequential with no alignment padding.** Reading column *k* means
walking columns 0..k−1 — O(k). Fine for a scan, which decodes whole rows
anyway, but a projection of the last column of a wide table still pays to walk.
PostgreSQL caches per-attribute offsets (`attcacheoff`) for the fixed-width
prefix of a tuple; SQLite puts every field's type-and-length in a header at the
front of the record so it can skip ahead. ChenDB will need one of these if wide
tables ever matter.

**Fixed-width integers.** Every integer costs 8 bytes even when it holds 1.
SQLite stores integers in the narrowest of 1/2/3/4/6/8 bytes and records the
choice per row — much smaller in practice, one branch per column in cost.
PostgreSQL takes the third road: distinct declared types, so width is a schema
decision resolved once.

**Little-endian.** Matches every realistic target, so encoding is a memory copy
rather than a byte swap. Big-endian would let integer keys be compared with
`memcmp`, which is genuinely useful for a B+ tree and is why RocksDB and
FoundationDB encode keys big-endian. Milestone 5 will compare decoded values
instead — a real cost, taken knowingly.

**4-byte text length.** Simple and uniform, three bytes of overhead per short
string. PostgreSQL uses a 1-byte header for values up to 126 bytes; SQLite uses
varints. On a table of short strings that is roughly 3 bytes per row per
column.

---

## Record identity

```python
RecordId(page_id=3, slot_id=4)      # rendered as (3,4)
```

PostgreSQL calls this a `ctid`. It survives page compaction (the slot directory
absorbs the move) but **not** a row moving to a different page. Milestone 5's
index stores these in its leaves, which is why an update that relocates a row
has to touch every index — PostgreSQL's HOT optimisation exists to dodge
precisely that cost.

---

## Page allocation and the free list

`Pager.allocate_page` prefers the free list; only if it is empty does the file
grow. A freed page is zeroed, typed `FREE`, and pushed onto the head of the
chain — the "next free" pointer lives in the page's own `next_page_id`, so the
free list costs no space outside the pages themselves.

```
 meta.free_list_head ──▶ page 7 ──▶ page 4 ──▶ page 9 ──▶ 0xFFFFFFFF
                        (FREE)     (FREE)     (FREE)
```

The file never shrinks. Reclaiming trailing pages requires proving nothing
points at them, which is what `VACUUM FULL` does in PostgreSQL and `VACUUM` in
SQLite.

Each allocation also rewrites the meta page, so it costs two writes. Real
systems amortise this: PostgreSQL's Free Space Map is a separate fork that is
buffered and only crash-*hinted*, not crash-safe.

---

## Checksums

Every page carries a CRC32 over its own bytes (excluding the checksum field),
refreshed on write and verified on read. A mismatch raises
`ChecksumMismatchError`.

This detects torn writes — the OS wrote part of a page before losing power —
and media corruption. It cannot *repair* anything: that needs the write-ahead
log in Milestone 9. Detection alone is still the difference between a loud
failure and silently wrong query results, which is why PostgreSQL ships
`data_checksums` and why ZFS checksums every block.

`Pager(verify_checksums=False)` exists so the inspector can render a damaged
page instead of refusing to open it.

---

## B+ tree pages

Milestone 5 adds two page types, `BTREE_INTERNAL` (4) and `BTREE_LEAF` (5).
Neither is a new kind of storage: both are ordinary slotted pages whose slots
hold index entries instead of table rows, so checksums, splits, the free list,
the disk map and the page inspector all work on them unchanged.

```
 leaf page                              internal page
 ┌──────────────────────────────┐       ┌──────────────────────────────┐
 │ header (24 B)                │       │ header (24 B)                │
 │   page_type = 5              │       │   page_type = 4              │
 │   next_page_id = next leaf ──┼──▶    │   next_page_id = 0xFFFFFFFF  │
 ├──────────────────────────────┤       ├──────────────────────────────┤
 │ slot directory, in KEY order │       │ slot directory, in KEY order │
 ├──────────────────────────────┤       ├──────────────────────────────┤
 │ key ‖ rid (6 B)              │       │ key ‖ rid (6 B) ‖ child (4 B)│
 │ …                            │       │ …                            │
 └──────────────────────────────┘       └──────────────────────────────┘
```

**Slot order is key order.** That is the one invariant the node layer exists to
maintain, and it is why `Page` grew `insert_at`/`delete_at`, which shift the slot
directory. Those two methods *renumber* slots, which is exactly right for an
index (nothing outside the node holds a slot id) and would be catastrophic for a
heap (every `RecordId` in the database would start pointing at the wrong row).
PostgreSQL draws the same line: `PageIndexTupleDelete` compacts the line pointer
array and is used only on index pages.

The leaf's sibling pointer reuses the header's `next_page_id`, the same field a
heap uses for its page chain. Safe, because a page belongs to exactly one
structure. Internal nodes leave it null: only leaves are walked sideways.

### Key encoding

Index keys are **not** the record encoding. `serialization/types.py` writes
integers little-endian, which does not sort by `memcmp`:

```
 value   little-endian              index key (big-endian, sign-flipped)
 ─────   ────────────────────────   ────────────────────────────────────
     1   01 00 00 00 00 00 00 00    02 80 00 00 00 00 00 00 01
   256   00 01 00 00 00 00 00 00    02 80 00 00 00 00 00 01 00
    -1   ff ff ff ff ff ff ff ff    02 7f ff ff ff ff ff ff ff
```

Byte-comparing the little-endian forms says `1 > 256`. The index encoding is
big-endian with `+2**63` applied, so unsigned byte order *is* signed value
order, and a comparison is one `memcmp` rather than two `struct.unpack` calls.
RocksDB and FoundationDB do the same, for the same reason.

Every key carries a 1-byte tag:

| tag | meaning | where |
|---|---|---|
| `0x00` | minus infinity | the first separator of every internal node |
| `0x01` | `NULL` | anywhere; sorts below all values (SQLite's order) |
| `0x02` | a value | payload follows |

| type | payload |
|---|---|
| INTEGER | 8 bytes big-endian of `value + 2**63` |
| FLOAT | 8 bytes big-endian IEEE-754, sign bit flipped (negatives: all bits) |
| BOOLEAN | 1 byte |
| TEXT | raw UTF-8, no length prefix — byte order is code-point order |

`-0.0` is normalised to `0.0` so a unique index cannot hold both. `NaN` sorts
above every real number, matching PostgreSQL.

### Why the record id is part of the key

Entries sort by `(key, record_id)`, not by key alone. Every entry in the tree is
therefore unique, which means a run of duplicates can be split anywhere, and
deleting one row's entry is a descent rather than a scan of every duplicate.
PostgreSQL adopted exactly this in version 12 and reported large reductions in
index bloat on low-cardinality columns.

They compare as a **pair**, not as concatenated bytes: `"ab" ‖ rid` and
`"abc" ‖ rid` interleave, and the outcome would depend on the numeric record id.
Splitting the fixed-width 6-byte suffix off by length is exact and needs no
escaping layer — which is also why Milestone 5 indexes one column and not two.

### Capacity

On a 4 KiB page an INTEGER leaf entry is 1 (tag) + 8 (value) + 6 (rid) = 15
bytes plus a 4-byte slot, so `(4096 − 24) / 19 ≈ 214` entries. Fanout ≈ 214, and
three levels address ≈ 9.8 million rows. A balanced *binary* tree over the same
data is 24 levels deep, so 24 page reads against 3 — the entire argument.

---

## Durability, honestly

`write_page` hands bytes to the operating system. It does **not** make them
durable — the OS may hold them in its page cache for seconds. Only `sync()`
(`fsync`) survives a power loss.

Milestone 1 therefore has a real crash window:

| Failure                        | Milestone 1                      | Milestone 9 |
|--------------------------------|----------------------------------|-------------|
| Process killed after `sync()`  | committed data survives          | same        |
| Process killed before `sync()` | recent rows may be lost          | redo from WAL |
| Torn page write                | detected by checksum, unreadable | repaired by redo |
| Crash mid-`create_table`       | orphaned page, table absent      | rolled back |

`tests/recovery/test_crash_and_corruption.py` pins each of these down by
`SIGKILL`ing a child process — no `close()`, no `fsync`, no atexit hooks. Any
cooperative shutdown would quietly flush the very buffers under test.

---

## Complexity

| Operation            | Cost                             | Notes |
|----------------------|----------------------------------|-------|
| `page.insert`        | O(1), O(slots) if it compacts    | in memory |
| `page.read(slot)`    | O(1)                             | one array index |
| `page.delete(slot)`  | O(1)                             | write a tombstone |
| `page.compact`       | O(slots + live bytes)            | |
| `pager.read_page`    | O(1) — 1 seek + 1 read           | a syscall until M7 |
| `pager.allocate`     | O(1) + 1 meta write              | |
| `heap.insert`        | O(1) — 1 read, 1–2 writes        | tail page only |
| `heap.get(rid)`      | O(1) — 1 read                    | |
| `heap.scan`          | O(pages) reads, O(rows) work     | |
| `heap.count`         | O(pages)                         | no cached count |
| `encode` / `decode`  | O(row bytes)                     | |
| `tree.search`        | O(log_f n) reads                 | *f* ≈ 214 at 4 KiB |
| `tree.insert`        | O(log_f n) reads, O(1) writes    | plus O(1) per split |
| `tree.delete`        | O(log_f n) reads, 1 write        | never merges |
| `tree.range_scan`    | O(log_f n) + matching leaves     | one descent, then sideways |
| `IndexScan`          | the above **plus one heap read per matching row** | the cost that decides whether it was worth it |

Every page read is a real syscall until Milestone 7's buffer pool.

---

## Where this design breaks down

- **Insert policy.** Only the tail page is tried, so space freed by deletes in
  earlier pages is never reused: a delete-heavy workload grows the file without
  bound. The fix is a free space map, and it becomes affordable once pages are
  buffered (Milestone 7).
- **Records larger than a page** simply fail. Needs overflow pages.
- **A linked-list heap** cannot jump to "page N" without walking, and defeats
  readahead in the general case. Ours stays near-sequential only because pages
  are appended.
- **One table per file.** Lifted by the catalog in Milestone 4.
- **No cached row count**, so `COUNT(*)` is a full scan — the same as
  PostgreSQL, and unlike MyISAM.
- **Meta page written on every allocation**: two writes per new page.
- **Wide tables** pay O(k) to reach column *k*.
- **No concurrency control.** One writer, no isolation. Milestones 8 and 10.
