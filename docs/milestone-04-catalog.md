# Milestone 4 — Persistent catalog and schema explorer

**Status: complete.** Engine version 0.4.0. **File format version 2.**

## Goal

Replace Milestone 1's single JSON schema page with a real catalog stored as
ordinary heap tuples — so a database can hold many tables, and adding one is an
`INSERT` rather than a change to the file format.

---

## The bootstrap problem

A catalog stores every table's schema. So what stores the *catalog's* schema?

To decode a row of `chendb_tables` you need its schema, which would live in
`chendb_tables`. Every real database solves this the same way: the system
tables' own definitions are **compiled into the engine**. PostgreSQL generates
them from `pg_class.h` at build time; SQLite hardcodes the shape of
`sqlite_schema` in `prepare.c`. ChenDB declares them in
`engine/catalog/system.py`.

```
┌─────────────────────────────────────────────────────────────┐
│ meta page (page 0)                                          │
│   catalog_tables_first  ──┐   catalog_columns_first  ──┐     │
└───────────────────────────┼───────────────────────────┼─────┘
                            ▼                           ▼
                 ┌──────────────────┐        ┌────────────────────┐
                 │ chendb_tables    │        │ chendb_columns     │
                 │  table_id        │        │  table_id          │
                 │  name            │        │  position          │
                 │  first_page ─────┼──▶     │  name              │
                 │  last_page       │        │  type_id           │
                 └──────────────────┘        │  nullable          │
                          │                  │  primary_key       │
                          ▼                  └────────────────────┘
                 a user table's heap
```

The meta page holds only the two pointers needed to *start* reading. Everything
else — including where every user table's heap begins — is a row.

### One deliberate difference from PostgreSQL

`pg_class` contains a row describing `pg_class`. Elegant, but it creates two
sources of truth for where the catalog lives: the row, and the bootstrap
pointer, which must never disagree. ChenDB synthesises the system tables'
descriptors from the compiled-in schemas plus the meta pointers, so each fact
lives in exactly one place. `test_the_system_tables_schemas_are_compiled_in_not_stored`
asserts `chendb_tables` holds no row about itself.

---

## Format version 2

The meta page changed, so version 1 files are **rejected on open** with a message
saying why rather than being reinterpreted through a layout they do not have.

| off | size | field | notes |
|---|---|---|---|
| 0 | 16 | magic | `ChenDB Format 1\0` |
| 16 | 4 | format_version | **2** |
| 20 | 4 | page_size | |
| 24 | 4 | page_count | |
| 28 | 4 | free_list_head | |
| 32 | 4 | catalog_tables_first | ← replaces `heap_first_page` |
| 36 | 4 | catalog_tables_last | ← replaces `heap_last_page` |
| 40 | 4 | catalog_columns_first | ← replaces `schema_page_id` |
| 44 | 4 | catalog_columns_last | |
| 48 | 4 | next_table_id | |
| 52 | 8 | lsn | reserved for M9 |
| 60 | 4 | flags | reserved |
| 64 | 4 | checksum | CRC32 over [0, 64) |

`PageType.SCHEMA` is retired but its value stays reserved, so it is never reused
for something else.

---

## The breaking API change

Every row-level method now takes a table name:

```python
db.insert("users", (1, "ada@example.com"))     # was db.insert(values)
db.rows("users")                               # was db.rows()
db.count("orders")                             # was db.count()
db.schema_of("users")                          # was db.schema
db.heap_for("users")                           # was db.heap
```

`/table` became `/tables` and `/records` became `/tables/{table}/records`.
`TableDescriptor` — the Milestone 1 JSON placeholder — is gone; `TableInfo` from
the catalog replaces it.

This is churn, and it is the right kind: the old signatures were only possible
*because* of a limitation that no longer exists.

---

## Decisions worth naming

**Column `position` is stored explicitly.** A heap scan returns *physical* order,
which is not column order — and after a delete lets a slot be reused, the two
genuinely diverge. `position` is what makes the rebuild correct rather than
lucky, and `_load_schema` rejects non-contiguous positions as corruption.

**Lookups are cached in memory, per open database.** A miss costs a full scan of
`chendb_tables` *plus* one of `chendb_columns` — O(tables + columns). Real systems
index their catalogs (`pg_class_relname_nsp_index`); ChenDB caches instead, and
Milestone 5 makes a real index possible. `CatalogLookupEvent.from_cache` makes
the hit rate visible, and the catalog panel shows it.

**Cache coherence by single writer.** Every mutation goes through `Catalog`,
which updates the cache in the same call that writes the rows. Nothing else may
write the system tables — `is_system_table` rejects `chendb_*` at both the SQL
and catalog layers, matched on the *prefix* so a future system table is protected
the moment it is named.

**`last_page` follows the heap.** When a table's heap extends, the
`chendb_tables` row is rewritten. If it went stale, a reopened database would
append into the *middle* of the chain — silently, and only for tables big enough
to have spilled. Two tests guard it.

**Rewriting that row is delete-then-insert.** The heap has no update operation,
because a row that grows may not fit where it was. The catalog's row is
fixed-width so in-place would work, but adding an update to the heap for the
catalog's sole benefit would be a special case a general update later has to
unpick.

**Row counts are computed, never cached.** `O(pages)` per request. Keeping an
exact count would cost a write on every insert, which is why PostgreSQL's
`reltuples` is an *estimate* maintained by `ANALYZE`.

**Table ids start at 100.** Ids 1 and 2 are the system tables; the gap leaves
room for future system tables — indexes in Milestone 5, sequences, constraints —
without renumbering anything already on disk.

---

## Complexity

| Operation | Cost |
|---|---|
| `get_table` (cache hit) | O(1) |
| `get_table` (miss) | O(tables) + O(columns) page reads |
| `list_tables` | O(tables) + O(columns) |
| `create_table` | O(tables) to check duplicates, then 1 + *n* inserts |
| heap extension | O(tables) to find and rewrite the row |
| row count | O(pages) |

The `create_table` and heap-extension scans are the ones that would hurt first at
scale, and both are exactly what a catalog index fixes.

---

## Tests

33 new catalog tests plus a rewritten persistence suite — **619 Python** (from
605), 62 frontend.

| File | Covers |
|---|---|
| `tests/unit/test_catalog.py` | bootstrap, self-description, ids, duplicates, reserved names, schema rebuild, caching, heap tracking, catalog spilling across pages |
| `tests/integration/test_persistence.py` | rewritten: many tables surviving a restart, column order, every column attribute, heap growth, system tables readable |
| `tests/integration/test_api.py` | `/catalog`, `/tables`, per-table records |

Four worth naming:

- **`test_column_order_comes_from_position_not_physical_order`** — 20 columns
  spanning pages, so physical order genuinely differs from column order.
- **`test_extending_a_heap_updates_its_catalog_row`** — the silent-corruption
  case above.
- **`test_many_tables_spill_the_catalog_across_pages`** — 40 tables, so the
  catalog's *own* heaps have to chain correctly.
- **`test_a_lookup_miss_costs_scans_and_a_hit_costs_none`** — proves the cache
  does what it claims.

---

## Acceptance criteria

- [x] Many tables per database, each with its own heap and schema.
- [x] Schemas rebuild from `chendb_columns` after a restart, with column order,
      types, nullability and primary keys intact.
- [x] The system tables are readable like any other table, and hidden by default.
- [x] Table ids are stable across restarts and never reuse a reserved value.
- [x] Duplicate and reserved table names are refused.
- [x] An unknown table lists the tables that do exist.
- [x] Lookups are case-insensitive; the declared case is preserved.
- [x] A heap that grows updates its catalog row, so reopening appends correctly.
- [x] Version 1 files are rejected with an explanation, not misread.
- [x] The catalog cache's hit rate is observable.
- [x] Per-table storage statistics come from walking the real page chain.

---

## Known limitations

| Limitation | Resolved by |
|---|---|
| Catalog lookups are full scans (cached, but cold is O(n)) | M5 — a catalog index |
| No `DROP TABLE`, so a heap is never reclaimed | unscheduled; needs the free list plus catalog deletes |
| No `ALTER TABLE` | unscheduled |
| Creating a table is not atomic — a crash mid-way can orphan a heap | M9 — the WAL |
| Row counts are O(pages) per request | unscheduled; needs `ANALYZE`-style estimates |
| No joins, so a query still reads one table | needs blocking operators |
| No schemas/namespaces | unscheduled |

---

## Next: Milestone 5 — B+ tree indexes and tree visualizer

**Engine.** A disk-backed B+ tree: point lookup, insert, node split, range scan,
linked leaves. `CREATE INDEX` starts parsing. Index metadata joins the catalog as
a third system table, which is exactly what the reserved id gap was for.

**Visualizer.** The real tree for an index, with search-path, split and
range-scan animation, plus node and page inspection.

**Demo.** Insert enough rows to force leaf, internal and root splits.
