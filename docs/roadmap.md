# Roadmap

Ten milestones. Each ends with a working database, a working visualizer, and a
demo. Nothing is stubbed ahead of time: a feature is absent from the API and
hidden in the UI until the engine behind it exists.

| # | Engine | Visualizer | Status |
|---|--------|------------|--------|
| 1 | Pages, slotted heap, records, persistence | Disk map, page inspector, event timeline | **done** |
| 2 | Tokenizer, recursive-descent parser, AST | Monaco editor, token stream, AST tree | **done** |
| 3 | Volcano operators: scan, filter, project | Operator tree, step-through, row inspector | **done** |
| 4 | Persistent catalog, multiple tables | Schema browser, storage statistics | **done** |
| 5 | Disk-backed B+ tree, `CREATE INDEX`, index scan | Real tree view, traced descent, node inspector | **done** |
| 6 | Logical + physical plans, statistics, cost model, `EXPLAIN` | Estimated vs actual, rejected alternatives | **done** |
| 7 | Buffer pool, write-back, LRU eviction | Frame grid, counters, workload runner | **done** |
| 8 | Transactions, undo log, rollback, atomic DDL | Transaction timeline, undo log, BEGIN/COMMIT/ROLLBACK | **done** |
| 9 | WAL, checkpoints, ARIES-style recovery | WAL table, crash button, step-through recovery | next |
| 10 | MVCC, locks, wait-for graph, deadlocks | Multi-session consoles, version chains, lock table | |

Engine version tracks the milestone: `0.N.0` means Milestone N is complete.

## What each milestone adds to the file format

| # | New page types | New meta fields |
|---|---|---|
| 1 | `META`, `HEAP`, `SCHEMA`, `FREE` | magic, version, page count, free list, heap/schema roots |
| 4 | — (catalog uses `HEAP`; `SCHEMA` retired) | **v2**: `catalog_tables_*`, `catalog_columns_*`, `next_table_id` replace the three M1 root pointers |
| 5 | `BTREE_INTERNAL`, `BTREE_LEAF` | **v3**: `catalog_indexes_*`; `next_table_id` becomes `next_object_id`, one id sequence for tables and indexes |
| 6 | — | — (statistics are in memory, not persisted — see `docs/milestone-06-planner.md`) |
| 7 | — | — (the pool is memory; the file format is untouched) |
| 8 | — | — (the undo log is memory; it dies with the process, which is why a crash mid-transaction is not atomic) |
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
