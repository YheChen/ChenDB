# Roadmap

Each milestone ends with a working database, a working visualizer, and a demo.
Nothing is stubbed ahead of time: a feature is absent from the API and hidden in
the UI until the engine behind it exists.

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
| 9 | WAL, checkpoints, ARIES-style recovery | WAL table, crash button, recovery report | **done** |
| 10 | MVCC, locks, wait-for graph, deadlocks | Two consoles, snapshots, lock table | **done** |
| 11 | `UPDATE ... SET`, `DELETE ... WHERE`, version chains | Update walkthroughs, live rows vs versions | **done** |
| 12 | — (CI over everything above) | One catalogue of demo SQL, checked against the engine | **done** |
| 13 | Joins, `GROUP BY`, aggregates, `ORDER BY`, `LIMIT` | Join trees, grouped planner decisions | **done** |
| 14 | — (a transport seam, so the app can carry the engine) | Same UI, no server required | **done** |
| 15 | — (the engine, compiled to WebAssembly) | The whole explorer in a browser tab, no backend | **done** |
| 16 | — (deployment and persistence) | Databases survive a refresh, kept in IndexedDB | **done** |
| 17 | Enforced primary keys, and seven bug fixes a differential tester found | — (a test suite, not a panel) | **done** |
| 18 | `LEFT`/`RIGHT`/`FULL OUTER JOIN`, and a join search that respects them | An outer-join demo, and the flavour named in every plan | **done** |

Engine version tracks the milestone: `0.N.0` means Milestone N is complete —
which runs out at ten, because there is no `0.10.0` that sorts after `0.9.0`.
So the tenth is `1.0.0`, the eleventh `1.1.0`, and `engine.MILESTONE` is
`major * 10 + minor`.

Not every milestone adds an engine feature. The twelfth added continuous
integration, which is a guarantee about the other eleven — see
`docs/milestone-12-ci.md` for why `MILESTONE_FEATURES` stopped at eleven
entries.

The seventeenth is the interesting case. It shipped a *test suite*, which is a
statement about the engine like CI — but one of the seven bugs it found was that
`PRIMARY KEY` had never been enforced, and fixing that gave the engine something
it could not do before. So it is a feature after all, and it is named rather
than excused.

Each milestone document ends with the honest edge of what it built; the one for
the newest is `docs/milestone-18-outer-joins.md`.

## What each milestone adds to the file format

| # | New page types | New meta fields |
|---|---|---|
| 1 | `META`, `HEAP`, `SCHEMA`, `FREE` | magic, version, page count, free list, heap/schema roots |
| 4 | — (catalog uses `HEAP`; `SCHEMA` retired) | **v2**: `catalog_tables_*`, `catalog_columns_*`, `next_table_id` replace the three M1 root pointers |
| 5 | `BTREE_INTERNAL`, `BTREE_LEAF` | **v3**: `catalog_indexes_*`; `next_table_id` becomes `next_object_id`, one id sequence for tables and indexes |
| 6 | — | — (statistics are in memory, not persisted — see `docs/milestone-06-planner.md`) |
| 7 | — | — (the pool is memory; the file format is untouched) |
| 8 | — | — (the undo log is memory; it dies with the process, which is why a crash mid-transaction is not atomic) |
| 9 | — | **v4**: `checkpoint_lsn` — the LSN of the log file's first byte. Page `lsn` starts being written; the field had been reserved since Milestone 1. |
| 10 | — | **v5**: `next_xid`; every row gains an 8-byte tuple header with `xmin`/`xmax` |
| 11 | — | — (an update writes a second version through the M10 header; nothing new on disk) |
| 12 | — | — (nothing runs; everything is checked) |
| 13 | — | — (joins are a planner and executor change; the file format is untouched) |
| 16 | — | — (IndexedDB stores the same bytes; a page keeps the checksum it was written with) |
| 17 | — | — (a `PRIMARY KEY` now builds a real B+ tree, but with the M5 page types and no new meta field) |
| 18 | — | — (outer joins are a planner and executor change; the file format is untouched) |

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
- **Bushy plans and index nested-loop joins.** Milestone 13 does left-deep
  plans; each of these is a real extension with a real reason it was left out.
  (Outer joins were on this list until Milestone 18.)
- **Reordering across an outer join, and outer-join simplification.** Milestone 18
  runs an outer join where it was written and never rewrites one to an inner join,
  even when a null-rejecting `WHERE` proves it is one. PostgreSQL's
  `min_lefthand`/`min_righthand` per outer join is how the first is recovered.
- **Subqueries, `DISTINCT`, `UNION`, window functions.**
- **`RETURNING`, and `INSERT ... SELECT`.** Both need a mutation to be an
  operator that emits rows rather than a call into storage. One refactor covers
  the pair; see `docs/milestone-11-dml.md`.
- **Heap-only tuples.** Milestone 11 rewrites every index on every update
  because the row's address changed. PostgreSQL's HOT avoids it when no indexed
  column changed and the new version fits on the same page.
