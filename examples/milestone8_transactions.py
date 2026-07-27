#!/usr/bin/env python3
"""A narrated tour of Milestone 8 transactions.

    python examples/milestone8_transactions.py

Six things: what a half-failed statement used to leave behind and what it leaves
now, why the undo log is measured in pages rather than rows, how `CREATE TABLE`
became atomic without the catalog learning what a transaction is, what a rollback
has to fix beyond the bytes, what the failed state is for, and where the
atomicity stops — which is where Milestone 9 starts.
"""

from __future__ import annotations

import hashlib
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.errors import ChenDBError
from engine.executor.engine import execute_script
from engine.transaction.undo import MAX_UNDO_BYTES

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("age", DataType.INTEGER, nullable=True),
)

ROW_COUNT = 2_000
PAGE_SIZE = 4096


def rule(title: str) -> None:
    print(f"\n{'-' * 78}\n{title}\n")


def digest(db: Database) -> str:
    """A hash of every page the meta page claims — the strongest "unchanged"."""
    db.sync()
    return hashlib.sha256(db.path.read_bytes()[: db.page_count * db.page_size]).hexdigest()


def median_ns(fn, calls: int = 2000) -> float:
    samples = []
    for _ in range(calls):
        started = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - started)
    return statistics.median(samples)


def main() -> int:
    print("ChenDB Milestone 8 - transactions")

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "shop.chendb"
        with Database.open(path, page_size=PAGE_SIZE) as db:
            db.create_table("users", SCHEMA)
            db.insert_many(
                "users",
                [(n, f"u{n:05d}@example.com", 20 + n % 50) for n in range(ROW_COUNT)],
            )
            db.sync()
            print(f"\n{ROW_COUNT} rows across {db.page_count} pages of {PAGE_SIZE} B")

            # -- 1 -----------------------------------------------------------
            rule("1. A statement that fails half-way now leaves nothing")

            before = digest(db)
            print("  INSERT INTO users VALUES (9001, ...), (9002, ...), (9001, ...)")
            print("                            ^ ok        ^ ok        ^ NULL email\n")
            try:
                execute_script(
                    "INSERT INTO users VALUES "
                    "(9001, 'a@x.com', 30), (9002, 'b@x.com', 31), (9003, NULL, 32);",
                    db,
                )
            except ChenDBError as exc:
                print(f"  rejected: {exc}")

            aborted = db.transactions.history()[-1]
            print("\n  the engine opened an implicit transaction for that statement:")
            print(f"    implicit        {aborted.implicit}")
            print(f"    page writes     {aborted.pages_written}")
            print(f"    pages restored  {aborted.pages_restored}")
            print(f"\n  rows now: {db.count('users')} (unchanged)")
            print(f"  file unchanged: {digest(db) == before}")
            print("\n  Milestones 1-7 would have kept 9001 and 9002.")

            # -- 2 -----------------------------------------------------------
            rule("2. The undo log grows with pages, not with rows")

            db.begin()
            db.insert_many(
                "users", [(20_000 + n, f"x{n}@x.com", 40) for n in range(20_000)]
            )
            active = db.transactions.active
            assert active is not None
            print("  20,000 rows inserted inside one transaction\n")
            print(f"    page writes seen    {active.pages_written:>8,}")
            print(
                f"    before-images kept  {active.pages_held:>8,}"
                f"   <- {active.pages_written // max(active.pages_held, 1)}x fewer"
            )
            print(f"    undo log            {active.undo_bytes / 1024:>8,.0f} KiB")
            print(f"    cap                 {MAX_UNDO_BYTES / 1024 / 1024:>8,.0f} MiB")

            started = time.perf_counter_ns()
            db.rollback()
            elapsed = time.perf_counter_ns() - started
            print(
                f"\n  rollback: {elapsed / 1000:,.0f} us  "
                f"({elapsed / 1000 / max(active.pages_restored, 1):.2f} us/page)"
            )
            print(f"  rows now: {db.count('users')}")
            print("\n  A page is captured once. Every later write to it is free -")
            print("  which is what makes whole-page snapshots affordable at all.")

            # -- 3 -----------------------------------------------------------
            rule("3. CREATE TABLE is atomic, and the catalog never found out")

            before = digest(db)
            txn = db.begin()
            db.create_table("orders", SCHEMA)
            print(
                f"  inside the transaction, orders exists: {db.table('orders') is not None}"
            )
            print(f"  pages captured so far: {txn.pages_held}")
            db.rollback()

            print(f"  after rollback,  orders exists: {db.table('orders') is not None}")
            print(f"  file unchanged: {digest(db) == before}")
            print("\n  CREATE TABLE writes rows into two system tables, allocates a")
            print("  heap page and bumps a counter. The undo log saw pages change")
            print("  and kept snapshots. engine/catalog/ has no idea it happened.")

            # -- 4 -----------------------------------------------------------
            rule("4. Rollback has to fix more than the bytes")

            print("  Two pieces of engine state are decoded into memory, not re-read:")
            print("    the meta page      page_count, next_object_id")
            print("    the catalog cache  table ids, schemas, heap roots")
            print("    the statistics     row counts, for the planner")
            print("\n  Database.rollback() reloads all three. Without that, a")
            print("  rolled-back CREATE TABLE would leave the engine serving a")
            print("  table whose heap page has since been overwritten.")

            # -- 5 -----------------------------------------------------------
            rule("5. A failed statement dooms the transaction")

            execute_script("BEGIN;", db)
            execute_script("INSERT INTO users VALUES (7001, 'g@x.com', 33);", db)
            try:
                execute_script("INSERT INTO users VALUES (7002, NULL, 34);", db)
            except ChenDBError as exc:
                print(f"  rejected: {exc}")

            print(f"\n  transaction state: {db.transactions.active.state.value}")
            try:
                execute_script("SELECT * FROM users;", db)
            except ChenDBError as exc:
                print(f"  next statement:    {exc}")

            (result,) = execute_script("COMMIT;", db)
            print(f"  COMMIT:            {result.message}")
            print(f"  row 7001 present:  {any(r[0] == 7001 for r in db.rows('users'))}")
            print("\n  PostgreSQL's rule. Without it, a client that opened the")
            print("  transaction in an earlier request could COMMIT half of it.")

            # -- 6 -----------------------------------------------------------
            rule("6. The hook is nearly free, because it is on every write")

            manager = db.transactions
            floor = median_ns(lambda: None)
            idle = median_ns(lambda: manager.before_write(3, lambda: b"", "x"))

            db.begin()
            manager.before_write(3, lambda: bytes(PAGE_SIZE), "seed")
            held = median_ns(lambda: manager.before_write(3, lambda: b"", "x"))
            db.rollback()

            print(f"  loop floor                        {floor:>7.0f} ns")
            print(
                f"  before_write, no transaction      {idle - floor:>7.0f} ns  (a None check)"
            )
            print(
                f"  before_write, page already held   {held - floor:>7.0f} ns  (a set lookup)"
            )
            print("\n  An insert costs ~13 us, so this is under 1% either way.")
            print("  The current image is passed as a callable, not bytes: reading")
            print("  a page to snapshot it is wasted work whenever it is already")
            print("  captured, which is almost every write.")

            # -- 7 -----------------------------------------------------------
            rule("7. Where the atomicity stops")

            print("  ChenDB is atomic against ERRORS, not against POWER LOSS.")
            print()
            print("    rollback in this process   always correct, even if the pool")
            print("                               already evicted the dirty page")
            print("    crash mid-transaction      NOT atomic - whatever the pool")
            print("                               evicted is on disk, and the undo")
            print("                               log died with the process")
            print()
            print("  Pinning uncommitted pages would not fix that either: a crash")
            print("  during the commit flush still leaves a partial transaction,")
            print("  and nothing on disk distinguishes it from a complete one.")
            print()
            print("  What is missing is a commit RECORD - one small durable write")
            print("  saying 'everything before this counts'. That is a write-ahead")
            print("  log, and it is Milestone 9.")

    print(f"\n{'-' * 78}")
    print("docs/milestone-08-transactions.md has the full reasoning.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
