"""The system catalog: what tables and indexes exist, and where they live.

    system.py    the system tables' own schemas, compiled in (the bootstrap)
    catalog.py   Catalog, create, look up and list tables and indexes

Milestone 4 replaced Milestone 1's single JSON schema page: a database can hold
many tables, and adding one is an ``INSERT`` into ``chendb_tables`` rather than a
change to the file format.  Milestone 5 adds ``chendb_indexes`` alongside it, so
an index is a catalog row plus a B+ tree root page.

    from engine import Database

    with Database.open("shop.chendb") as db:
        db.create_table("users", users_schema)
        db.create_index("users_email", "users", "email", unique=True)
        print(db.table_names())            # ['users']
        print(db.lookup("users_email", "ada@example.com"))
        print(db.catalog.stats.hit_rate)   # cache effectiveness
"""

from engine.catalog.catalog import Catalog, CatalogStats, IndexInfo, TableInfo
from engine.catalog.system import (
    COLUMNS_TABLE_ID,
    COLUMNS_TABLE_NAME,
    COLUMNS_TABLE_SCHEMA,
    FIRST_USER_OBJECT_ID,
    INDEXES_TABLE_ID,
    INDEXES_TABLE_NAME,
    INDEXES_TABLE_SCHEMA,
    SYSTEM_TABLE_NAMES,
    SYSTEM_TABLE_PREFIX,
    TABLES_TABLE_ID,
    TABLES_TABLE_NAME,
    TABLES_TABLE_SCHEMA,
    is_system_table,
)

__all__ = [
    "COLUMNS_TABLE_ID",
    "COLUMNS_TABLE_NAME",
    "COLUMNS_TABLE_SCHEMA",
    "FIRST_USER_OBJECT_ID",
    "INDEXES_TABLE_ID",
    "INDEXES_TABLE_NAME",
    "INDEXES_TABLE_SCHEMA",
    "SYSTEM_TABLE_NAMES",
    "SYSTEM_TABLE_PREFIX",
    "TABLES_TABLE_ID",
    "TABLES_TABLE_NAME",
    "TABLES_TABLE_SCHEMA",
    "Catalog",
    "CatalogStats",
    "IndexInfo",
    "TableInfo",
    "is_system_table",
]
