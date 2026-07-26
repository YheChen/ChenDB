"""The system catalog: what tables exist, and where their rows live.

    system.py    the system tables' own schemas, compiled in (the bootstrap)
    catalog.py   Catalog — create, look up and list tables

Milestone 4 replaces Milestone 1's single JSON schema page. A database can now
hold many tables, and adding one is an ``INSERT`` into ``chendb_tables`` rather
than a change to the file format.

    from engine import Database

    with Database.open("shop.chendb") as db:
        db.create_table("users", users_schema)
        db.create_table("orders", orders_schema)
        print(db.table_names())            # ['orders', 'users']
        print(db.catalog.stats.hit_rate)   # cache effectiveness
"""

from engine.catalog.catalog import Catalog, CatalogStats, TableInfo
from engine.catalog.system import (
    COLUMNS_TABLE_ID,
    COLUMNS_TABLE_NAME,
    COLUMNS_TABLE_SCHEMA,
    FIRST_USER_TABLE_ID,
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
    "FIRST_USER_TABLE_ID",
    "SYSTEM_TABLE_NAMES",
    "SYSTEM_TABLE_PREFIX",
    "TABLES_TABLE_ID",
    "TABLES_TABLE_NAME",
    "TABLES_TABLE_SCHEMA",
    "Catalog",
    "CatalogStats",
    "TableInfo",
    "is_system_table",
]
