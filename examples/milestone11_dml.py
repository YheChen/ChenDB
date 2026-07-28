#!/usr/bin/env python3
"""A narrated tour of Milestone 11: UPDATE and DELETE.

    python examples/milestone11_dml.py

Six things: what an update physically does to a page, why that makes a version
chain two links long for the first time in this project, what it costs an index,
why the rows have to be found before any of them are changed, what the planner
now gets to decide about a DELETE, and where it all stops.

The short version: **an update is a delete and an insert.** Everything
surprising below follows from that one sentence.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.concurrency.snapshot import IsolationLevel
from engine.executor.engine import execute_script
from engine.planner.physical import PhysicalIndexScan, walk_physical
from engine.serialization.record import read_tuple_header

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
    Column("pay", DataType.INTEGER),
)
STAFF = [
    (1, "ada", 40_000),
    (2, "alan", 45_000),
    (3, "grace", 60_000),
    (4, "edsger", 75_000),
]
PAGE_SIZE = 4096


def rule(title: str) -> None:
    print(f"\n{'-' * 78}\n{title}\n")


def run(db: Database, sql: str):
    return execute_script(sql, db)[-1]


def versions(db: Database) -> list[str]:
    """Every version on the pages, live or not, in physical order."""
    out = []
    for record_id, payload in db.heap_for("staff").scan():
        header = read_tuple_header(payload)
        row = db.get("staff", record_id)
        mark = "dead" if header.deleted else "LIVE"
        out.append(
            f"    {mark}  {record_id}  xmin={header.xmin:<3} xmax={header.xmax:<3}  {row}"
        )
    return out


def main() -> int:
    print(__doc__)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "staff.chendb"

        with Database.open(path, page_size=PAGE_SIZE) as db:
            db.create_table("staff", SCHEMA)
            db.insert_many("staff", STAFF)

            # -- 1 ---------------------------------------------------------
            rule("1. An update does not edit a row")

            print("  Before:")
            print("\n".join(versions(db)))

            raise_ada = "UPDATE staff SET pay = pay + 5000 WHERE name = 'ada'"
            print(f"\n  {raise_ada}")
            print(f"  -> {run(db, raise_ada).message}")

            print("\n  After:")
            print("\n".join(versions(db)))

            print("\n  The old version is still on the page, with an xmax naming")
            print("  the transaction that ended it. The new one is appended, with")
            print("  the same transaction as its xmin. One statement, two writes,")
            print("  and the row's ADDRESS changed - which is the fact the next")
            print("  two sections are both about.")

            # -- 2 ---------------------------------------------------------
            rule("2. So an older reader still sees the old row")

            with db.in_session("alice"):
                db.begin(isolation=IsolationLevel.REPEATABLE_READ)
                alice_saw = {row[1]: row[2] for row in db.rows("staff")}

            with db.in_session("bob"):
                db.begin()
                run(db, "UPDATE staff SET pay = 1 WHERE name = 'grace'")
                db.commit()

            with db.in_session("alice"):
                still = {row[1]: row[2] for row in db.rows("staff")}
                print("  alice took a snapshot, then bob set grace's pay to 1.")
                print(f"    alice still reads  grace = {still['grace']}")
                print(f"    and she read       grace = {alice_saw['grace']}  before")
                db.commit()
                after = {row[1]: row[2] for row in db.rows("staff")}
                print(f"    after her COMMIT   grace = {after['grace']}")

            print("\n  This is the case MVCC was designed for and the first time in")
            print("  this project it could happen: until now a row was inserted")
            print("  once and deleted once, so 'the previous version' was always")
            print("  the same as 'no row'.")

            # -- 3 ---------------------------------------------------------
            rule("3. What it costs an index")

            db.create_index("name_idx", "staff", "name")
            print("  CREATE INDEX name_idx ON staff(name)   -- not on `pay`")
            print("  UPDATE staff SET pay = 2 WHERE name = 'alan'")
            run(db, "UPDATE staff SET pay = 2 WHERE name = 'alan'")
            found = db.lookup("name_idx", "alan")
            print(f"  -> lookup('alan') = {found}")
            print("\n  The update touched no indexed column, and the index still")
            print("  had to be rewritten: its entry pointed at the old slot and")
            print("  the live row is somewhere else now. An update to a table with")
            print("  four indexes is four B+ tree deletes and four inserts on top")
            print("  of two heap writes.")
            print("\n  This is exactly what PostgreSQL's heap-only tuples avoid: if")
            print("  no indexed column changed AND the new version fits on the same")
            print("  page, it chains the old version to the new one and leaves every")
            print("  index alone. ChenDB has no HOT, so it pays in full every time.")

            # -- 4 ---------------------------------------------------------
            rule("4. Why the rows are found before any of them change")

            # A table of its own, so the numbers below are only about this.
            db.create_table("payroll", SCHEMA)
            db.insert_many(
                "payroll", [(n, f"p{n}", 40_000 + n * 1000) for n in range(1, 5)]
            )
            print("  payroll holds " + str(sorted(r[2] for r in db.rows("payroll"))))
            print("  UPDATE payroll SET pay = pay + 2000 WHERE pay < 50000")
            print(
                f"  -> {run(db, 'UPDATE payroll SET pay = pay + 2000 WHERE pay < 50000').message}"
            )
            print(f"  -> it now holds {sorted(r[2] for r in db.rows('payroll'))}")
            print("\n  Every one of them is STILL under 50,000. If the scan were")
            print("  still running while the writes happened it would reach the new")
            print("  versions - a transaction always sees its own writes - and raise")
            print("  them again, and again, until they escaped the predicate.")
            print("\n  That is the Halloween problem, found at IBM on 31 October 1976")
            print("  and named after the day. ChenDB drains the row source into a")
            print("  list before it writes anything; SQL Server inserts an explicit")
            print("  'Eager Spool' operator into the plan to do the same job.")

            # -- 5 ---------------------------------------------------------
            rule("5. A DELETE is a query first")

            db.create_index("pay_idx", "staff", "pay")
            db.analyze("staff")
            result = run(db, "EXPLAIN DELETE FROM staff WHERE pay = 75000")
            for row in result.rows:
                print(f"    {row[0]}")
            planned = run(db, "EXPLAIN DELETE FROM staff WHERE pay = 75000").planned
            chose_index = any(
                isinstance(node, PhysicalIndexScan) for node in walk_physical(planned.root)
            )
            print(f"\n  Index scan chosen: {chose_index}")
            print("\n  Finding the rows is the half of a DELETE that has more than")
            print("  one right answer, so it goes through the same planner a SELECT")
            print("  does. Without that, deleting one row out of a million would")
            print("  read all million.")

            # -- 6 ---------------------------------------------------------
            rule("6. What it costs, and where it stops")

            # Enough rows that the one fsync per statement amortises away and
            # what is left is the per-row work.
            rows = 2_000
            db.create_table("bulk", SCHEMA)
            db.insert_many("bulk", [(n, f"n{n}", n) for n in range(rows)])
            db.create_index("bulk_name", "bulk", "name")
            db.create_index("bulk_pay", "bulk", "pay")

            started = time.perf_counter_ns()
            run(db, "UPDATE bulk SET pay = pay + 1")
            update_ns = (time.perf_counter_ns() - started) / rows

            started = time.perf_counter_ns()
            run(db, "DELETE FROM bulk")
            delete_ns = (time.perf_counter_ns() - started) / rows

            print(f"  over {rows} rows, two indexes:")
            print(f"    UPDATE   {update_ns / 1000:>7.1f} \u00b5s/row")
            print(f"    DELETE   {delete_ns / 1000:>7.1f} \u00b5s/row")
            print("\n  An update costs more than a delete for a structural reason,")
            print("  not an incidental one: the delete writes eight bytes of header")
            print("  and removes an index entry; the update does all of that AND")
            print("  encodes a whole new row AND puts an entry back.")

            print("\n  Where it stops:")
            print("    - no RETURNING, so a statement reports counts, not rows.")
            print("    - no EvalPlanQual. If another session replaces a row between")
            print("      this statement finding it and reaching it, the row is")
            print("      SKIPPED and the skip is reported. PostgreSQL follows the")
            print("      version chain and re-checks the predicate instead.")
            print("    - no UPDATE ... FROM and no DELETE ... USING: both need a")
            print("      second row source, and there are no joins yet.")
            print("    - no HOT, so every index is rewritten on every update.")
            print("    - the whole matched set is buffered in memory, so a")
            print("      statement matching more rows than the ceiling is refused")
            print("      rather than half-applied.")

    print(f"\n{'-' * 78}")
    print("docs/milestone-11-dml.md has the full reasoning.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
