# Roadmap

Ten milestones. Each ends with a working database, a working visualizer, and a
demo. Nothing is stubbed ahead of time: a feature is absent from the API and
hidden in the UI until the engine behind it exists.

| # | Engine | Visualizer | Status |
|---|--------|------------|--------|
| 1 | Pages, slotted heap, records, persistence | Disk map, page inspector, event timeline | **done** |
| 2 | Tokenizer, recursive-descent parser, AST | Monaco editor, token stream, AST tree | next |
| 3 | Volcano operators: scan, filter, project | Operator tree, step-through, row inspector | |
| 4 | Persistent catalog, multiple tables | Schema browser, storage statistics | |
| 5 | Disk-backed B+ tree | Real tree view, search/split/range animation | |
| 6 | Binder, logical + physical plans, cost model | Plan comparison, rejected alternatives | |
| 7 | Buffer pool, pinning, LRU eviction | Frame grid, hit/miss animation, workloads | |
| 8 | Transactions, undo records, rollback | Transaction timeline, before/after records | |
| 9 | WAL, checkpoints, ARIES-style recovery | WAL table, crash button, step-through recovery | |
| 10 | MVCC, locks, wait-for graph, deadlocks | Multi-session consoles, version chains, lock table | |

Engine version tracks the milestone: `0.N.0` means Milestone N is complete.

## What each milestone adds to the file format

| # | New page types | New meta fields |
|---|---|---|
| 1 | `META`, `HEAP`, `SCHEMA`, `FREE` | magic, version, page count, free list, heap/schema roots |
| 4 | — (catalog uses `HEAP`) | `catalog_root_page` replaces the three M1 root pointers |
| 5 | `BTREE_INTERNAL`, `BTREE_LEAF` | — (index roots live in the catalog) |
| 9 | — | `checkpoint_lsn`, `last_lsn`; page `lsn` starts being written |
| 10 | — | tuple headers gain `xmin`/`xmax` |

`FORMAT_VERSION` is bumped whenever any of this changes.

## Deliberately unscheduled

Real problems this design has, with no milestone assigned:

- **Overflow pages** for records larger than one page. Reserved as
  `PageType.OVERFLOW`; PostgreSQL's answer is TOAST, SQLite's is cell overflow.
- **A free space map** so inserts can reuse space freed anywhere, not just on
  the tail page. Only affordable once pages are buffered (M7).
- **Cached per-attribute offsets** so reaching column *k* is not O(k).
- **Compact integer encoding** — SQLite stores an integer in the narrowest of
  1/2/3/4/6/8 bytes.
- **`VACUUM`** to return trailing pages to the filesystem.
- **Joins, aggregation, `GROUP BY`, subqueries.** Milestone 3 builds the
  operator framework these would slot into.
