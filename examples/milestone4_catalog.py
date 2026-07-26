#!/usr/bin/env python3
"""A narrated tour of the Milestone 4 catalog.

    python examples/milestone4_catalog.py

Shows the bootstrap problem, many tables in one file, and schemas rebuilt from
disk after a restart.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.catalog.system import (
    COLUMNS_TABLE_NAME,
    COLUMNS_TABLE_SCHEMA,
    TABLES_TABLE_NAME,
    TABLES_TABLE_SCHEMA,
)
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.executor import execute_script

USERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("age", DataType.INTEGER),
)
ORDERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("user_id", DataType.INTEGER, nullable=False),
    Column("total", DataType.FLOAT),
)


def heading(number: int, text: str) -> None:
    print(f"\n\033[1m{number}. {text}\033[0m")
    print("─" * 78)


def main() -> int:
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "shop.chendb"
        sink = RingBufferSink()
        tracer = Tracer(sink, TraceLevel.STORAGE)

        # ------------------------------------------------------------------
        heading(1, "A new database is already a catalog")
        with Database.open(path, page_size=256, tracer=tracer) as db:
            print(f"   pages: {db.page_count}  (meta + one heap per system table)")
            print(f"   user tables: {db.table_names()}")
            print(f"   system: {[t.name for t in db.tables(include_system=True)]}")
            print("\n   Their schemas are COMPILED IN, not stored — decoding a")
            print("   chendb_tables row would otherwise need chendb_tables.")
            for name, schema in [
                (TABLES_TABLE_NAME, TABLES_TABLE_SCHEMA),
                (COLUMNS_TABLE_NAME, COLUMNS_TABLE_SCHEMA),
            ]:
                print(f"     {name:<16} ({', '.join(schema.column_names)})")

            # --------------------------------------------------------------
            heading(2, "Two tables in one file")
            db.create_table("users", USERS)
            db.create_table("orders", ORDERS)
            db.insert_many("users", [(1, "ada@x.com", 36), (2, "alan@x.com", None)])
            db.insert_many("orders", [(1, 1, 9.99), (2, 1, 24.5), (3, 2, 5.0)])
            for info in db.tables():
                print(
                    f"   {info.name:<8} id={info.table_id} "
                    f"pages={info.first_page}..{info.last_page} "
                    f"rows={db.count(info.name)}"
                )

            # --------------------------------------------------------------
            heading(3, "The catalog is just rows")
            print(f"   {TABLES_TABLE_NAME}:")
            for row in db.rows(TABLES_TABLE_NAME):
                print(f"     table_id={row[0]:<4} name={row[1]:<8} pages={row[2]}..{row[3]}")
            print(f"\n   {COLUMNS_TABLE_NAME} (first 6):")
            for row in db.rows(COLUMNS_TABLE_NAME)[:6]:
                print(
                    f"     table={row[0]} pos={row[1]} {row[2]:<8} "
                    f"type={DataType(row[3]).sql_name:<8} "
                    f"null={row[4]!s:<5} pk={row[5]}"
                )
            print("\n   chendb_tables holds no row about ITSELF: that would be two")
            print("   sources of truth for where the catalog lives. PostgreSQL's")
            print("   pg_class does; ChenDB synthesises system descriptors instead.")

        # ------------------------------------------------------------------
        heading(4, "Restart: everything comes back from disk")
        with Database.open(path, tracer=tracer) as db:
            for info in db.tables():
                columns = ", ".join(
                    f"{c.name} {c.data_type.sql_name}"
                    f"{'' if c.nullable else ' NOT NULL'}"
                    f"{' PK' if c.primary_key else ''}"
                    for c in info.schema
                )
                print(f"   {info.name:<8} ({columns})")
            print(f"\n   users : {db.rows('users')}")
            print(f"   orders: {db.rows('orders')}")

            # --------------------------------------------------------------
            heading(5, "Column order comes from `position`, not physical order")
            print("   A heap scan returns physical order, which diverges from column")
            print("   order once a deleted slot is reused. Storing `position` is what")
            print("   makes the rebuild correct rather than lucky.")
            print(f"   users column order: {db.schema_of('users').column_names}")

            # --------------------------------------------------------------
            heading(6, "SQL across tables, and a helpful failure")
            for sql in [
                "SELECT email FROM users WHERE id = 1",
                "SELECT total FROM orders WHERE user_id = 1",
                "SELECT * FROM customers",
            ]:
                try:
                    result = execute_script(sql, db)[0]
                    print(f"   {sql:<42} → {[tuple(r) for r in result.rows]}")
                except Exception as exc:
                    print(f"   {sql:<42} → {type(exc).__name__}: {exc}")

            # --------------------------------------------------------------
            heading(7, "The cache is why lookups are not O(tables) every time")
            db.catalog.invalidate()
            db.require_table("users")
            scans_after_miss = db.catalog.stats.scans
            for _ in range(50):
                db.require_table("users")
            print(f"   scans after one cold lookup: {scans_after_miss}")
            print(f"   scans after 50 more:         {db.catalog.stats.scans}")
            print(f"   hit rate:                    {db.catalog.stats.hit_rate:.0%}")
            print("\n   A miss costs a full scan of chendb_tables AND chendb_columns.")
            print("   Real systems index their catalogs; Milestone 5 makes that possible.")

        lookups = sum(
            1 for item in sink.snapshot() if item.event_type == "CatalogLookupEvent"
        )
        print(f"\n   catalog lookup events recorded: {lookups}")

    print("\n" + "─" * 78)
    print("Try it in the browser: python -m engine.server, then the Storage tab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
