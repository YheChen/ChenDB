"""The system tables, and the bootstrap problem they create.

A catalog stores every table's schema.  So what stores the *catalog's* schema?

That is a genuine chicken-and-egg: to decode a row of ``chendb_tables`` you need
its schema, which would live in ``chendb_tables``.  Every real database solves it
the same way — the system tables' own definitions are **compiled into the
engine** rather than read from disk.  PostgreSQL generates them from
``pg_class.h`` and friends at build time; SQLite hardcodes the shape of
``sqlite_schema`` in ``prepare.c``.  ChenDB declares them here.

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

The meta page holds only the two pointers needed to *start* reading. Everything
else — including where every user table's heap begins — is a row in
``chendb_tables``, which is the whole point: adding a table is an insert, not a
file-format change.

One deliberate difference from PostgreSQL: ``chendb_tables`` does **not** contain
rows describing itself. PostgreSQL's ``pg_class`` does have a row for
``pg_class``, which is elegant but creates two sources of truth for where the
catalog lives — the row and the bootstrap pointer — that must never disagree.
ChenDB synthesises the system tables' descriptors from the declarations below
plus the meta pointers, so there is exactly one place each fact lives.
"""

from __future__ import annotations

from typing import Final

from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

__all__ = [
    "COLUMNS_TABLE_ID",
    "COLUMNS_TABLE_NAME",
    "COLUMNS_TABLE_SCHEMA",
    "FIRST_USER_TABLE_ID",
    "SYSTEM_TABLE_NAMES",
    "SYSTEM_TABLE_PREFIX",
    "TABLES_TABLE_ID",
    "TABLES_TABLE_NAME",
    "TABLES_TABLE_SCHEMA",
    "is_system_table",
]

#: Reserved so a user table can never collide with a system one.
SYSTEM_TABLE_PREFIX: Final = "chendb_"

TABLES_TABLE_NAME: Final = "chendb_tables"
COLUMNS_TABLE_NAME: Final = "chendb_columns"

#: Fixed ids, like PostgreSQL's hardcoded OIDs for the bootstrap catalogs.
TABLES_TABLE_ID: Final = 1
COLUMNS_TABLE_ID: Final = 2

#: User tables start well above the reserved range, leaving room for future
#: system tables (indexes in Milestone 5, sequences, constraints) without
#: renumbering anything already written to disk.
FIRST_USER_TABLE_ID: Final = 100

SYSTEM_TABLE_NAMES: Final[frozenset[str]] = frozenset(
    {TABLES_TABLE_NAME, COLUMNS_TABLE_NAME}
)


#: One row per table. ``first_page``/``last_page`` are what make a table
#: reachable, so this is the closest thing ChenDB has to a directory.
TABLES_TABLE_SCHEMA: Final[Schema] = Schema.of(
    Column("table_id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
    Column("first_page", DataType.INTEGER, nullable=False),
    Column("last_page", DataType.INTEGER, nullable=False),
)

#: One row per column of every table. ``position`` is the column's index in its
#: table, which is also its position in an encoded record — the link between the
#: catalog and the on-disk row format.
COLUMNS_TABLE_SCHEMA: Final[Schema] = Schema.of(
    Column("table_id", DataType.INTEGER, nullable=False),
    Column("position", DataType.INTEGER, nullable=False),
    Column("name", DataType.TEXT, nullable=False),
    Column("type_id", DataType.INTEGER, nullable=False),
    Column("nullable", DataType.BOOLEAN, nullable=False),
    Column("primary_key", DataType.BOOLEAN, nullable=False),
)


def is_system_table(name: str) -> bool:
    """Whether ``name`` belongs to the engine rather than the user.

    Matched on the prefix, not the known set, so a future system table is
    protected the moment it is named — and a user cannot squat on the name
    before it exists.
    """
    return name.casefold().startswith(SYSTEM_TABLE_PREFIX)
