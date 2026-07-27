#!/usr/bin/env python3
"""A narrated tour of Milestone 10: MVCC, locks and deadlocks.

    python examples/milestone10_mvcc.py

Seven things: what a row version costs, why a reader never waits, what the two
isolation levels actually differ by, what a delete leaves behind and what
cleans it up, what two writers do when they collide, what happens when the
collision is circular, and where the whole thing stops.

Everything is two named sessions on one database handle — ``alice`` and ``bob``
— because that is how the explorer's two consoles work and because a
demonstration of concurrency with one participant is not one.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.concurrency.snapshot import IsolationLevel
from engine.errors import DeadlockError, LockTimeout
from engine.serialization.record import TUPLE_HEADER_SIZE

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)
PAGE_SIZE = 4096


def rule(title: str) -> None:
    print(f"\n{'-' * 78}\n{title}\n")


def ids(db: Database) -> list[int]:
    return sorted(row[0] for row in db.rows("t"))


def rid_of(db: Database, key: int):
    return next(record_id for record_id, row in db.scan("t") if row[0] == key)


def median_ns(fn, calls: int = 2000) -> float:
    samples = []
    for _ in range(calls):
        started = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - started)
    return statistics.median(samples)


def main() -> int:
    print("ChenDB Milestone 10 - MVCC, locks and deadlocks")

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "shop.chendb"
        with Database.open(path, page_size=PAGE_SIZE) as db:
            db.create_table("t", SCHEMA)
            db.insert_many("t", [(n, f"row{n:04d}") for n in range(5)])
            db.sync()

            # -- 1 ---------------------------------------------------------
            rule("1. What a row version costs")

            payload_size = db.describe("t", rid_of(db, 0)).total_size
            print(f"  tuple header      {TUPLE_HEADER_SIZE:>4} bytes  (xmin u32, xmax u32)")
            print(f"  a row here        {payload_size:>4} bytes")
            print(
                f"  overhead          {TUPLE_HEADER_SIZE / (payload_size + TUPLE_HEADER_SIZE) * 100:>4.0f}%"
                "     PostgreSQL's header is 23 bytes"
            )
            print("\n  Paid by every row, whether or not anything ever reads")
            print("  concurrently. It buys the thing that cannot be bought any")
            print("  other way, which is section 2.")

            # -- 2 ---------------------------------------------------------
            rule("2. A reader does not wait for a writer")

            with db.in_session("bob"):
                db.begin()
                db.insert("t", (100, "bob's row"))
                bob_locks = len(
                    db.locks.held_by(db.transactions.active_in("bob").transaction_id)
                )

            print(f"  bob has an open transaction holding {bob_locks} row lock(s).")
            started = time.perf_counter_ns()
            with db.in_session("alice"):
                alice_sees = ids(db)
                alice_locks = len(db.locks.held_by(0))
            elapsed = time.perf_counter_ns() - started

            print(f"  alice SELECTs anyway: {alice_sees}")
            print(
                f"  …in {elapsed / 1000:.0f} us, taking {alice_locks} lock(s), never blocking."
            )
            print("\n  She read an OLDER VERSION rather than waiting for the newer")
            print("  one. Bob's row is on the page; her snapshot cannot see it.")

            with db.in_session("bob"):
                print(f"  bob sees his own write: {100 in ids(db)}")
                db.commit()
            with db.in_session("alice"):
                print(f"  and once he commits, so does she: {100 in ids(db)}")

            # -- 3 ---------------------------------------------------------
            rule("3. The two isolation levels differ by one thing")

            with db.in_session("alice"):
                alice = db.begin(isolation=IsolationLevel.REPEATABLE_READ)
                before = ids(db)
            with db.in_session("bob"):
                db.begin()
                db.insert("t", (200, "later"))
                db.commit()
            with db.in_session("alice"):
                during = ids(db)
                for _ in range(2):
                    ids(db)
                taken = alice.snapshots_taken
                db.commit()
                after = ids(db)

            print(f"  repeatable read: before {before}")
            print(f"                   after bob commits {during}   unchanged")
            print(f"                   after alice commits {after}")
            print(f"                   snapshots taken across 4 statements: {taken}")

            with db.in_session("carol"):
                carol = db.begin(isolation=IsolationLevel.READ_COMMITTED)
                first = ids(db)
                with db.in_session("bob"):
                    db.begin()
                    db.insert("t", (300, "newer"))
                    db.commit()
                second = ids(db)
                print(f"\n  read committed:  before {first}")
                print(f"                   after bob commits {second}   changed")
                print(f"                   snapshots taken: {carol.snapshots_taken}")
                db.commit()

            print("\n  One snapshot per transaction, or one per statement. That is")
            print("  the entire difference, and it is one branch in the manager.")

            # -- 4 ---------------------------------------------------------
            rule("4. A delete leaves the version behind")

            # Alice's snapshot has to be taken BEFORE the delete, or she has
            # already seen it happen and the version is dead for her too.
            with db.in_session("alice"):
                db.begin(isolation=IsolationLevel.REPEATABLE_READ)
                ids(db)  # pin the horizon here

            db.delete("t", rid_of(db, 3))
            print(f"  visible rows      {db.count('t')}")
            print(f"  versions on disk  {db.version_count('t')}")
            print("\n  The slot is still live and the row still decodes. Only its")
            print("  xmax says it is gone — because a transaction whose snapshot")
            print("  predates the delete still has to be able to read it.")

            with db.in_session("alice"):
                print(
                    f"\n  and alice, whose snapshot predates it, still does: {3 in ids(db)}"
                )

            reclaimed = db.vacuum("t")
            print(f"  vacuum while she is open:          {reclaimed} reclaimed")
            with db.in_session("alice"):
                db.commit()
            reclaimed = db.vacuum("t")
            print(f"  vacuum once she has gone:          {reclaimed} reclaimed")
            print(f"  versions on disk now               {db.version_count('t')}")
            print("\n  A long-running transaction holds the horizon down and stops")
            print("  vacuum making progress. That is PostgreSQL's most common")
            print("  'why is my disk full', arrived at by the same mechanism.")

            # -- 5 ---------------------------------------------------------
            rule("5. Two writers on one row do collide")

            with db.in_session("bob"):
                db.begin()
                db.delete("t", rid_of(db, 1))
                held = next(
                    iter(db.locks.held_by(db.transactions.active_in("bob").transaction_id))
                )

            with db.in_session("alice"):
                alice = db.begin()
                print(f"  bob holds {held}")
                print(f"  alice can still READ row 1: {1 in ids(db)}")
                started = time.perf_counter_ns()
                try:
                    db.locks.acquire(alice.transaction_id, held, timeout=0.3)
                except LockTimeout:
                    waited = (time.perf_counter_ns() - started) / 1e6
                    print(
                        f"  …but waits {waited:.0f} ms trying to WRITE it, then gives up."
                    )
                db.rollback()
            with db.in_session("bob"):
                db.rollback()

            print("\n  This is the one conflict snapshot isolation cannot make")
            print("  disappear. Row granularity, not page - two writers on")
            print("  DIFFERENT rows of the same page do not meet at all.")

            # -- 6 ---------------------------------------------------------
            rule("6. When the collision is circular")

            locks = db.locks
            locks.acquire(9001, "t:demo.a")
            locks.acquire(9002, "t:demo.b")
            outcome: dict[int, str] = {}

            def younger() -> None:
                try:
                    locks.acquire(9002, "t:demo.a", timeout=3)
                    outcome[9002] = "granted"
                except DeadlockError:
                    outcome[9002] = "rolled back"
                    locks.release_all(9002)

            thread = threading.Thread(target=younger)
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and locks.wait_for_graph().get(9002) != {
                9001
            }:
                time.sleep(0.005)

            print(f"  wait-for graph: {dict(locks.wait_for_graph())}")
            try:
                locks.acquire(9001, "t:demo.b", timeout=3)
                outcome[9001] = "granted"
            except DeadlockError:
                outcome[9001] = "rolled back"
            thread.join(4)

            print(f"  9001 (older):   {outcome[9001]}")
            print(f"  9002 (younger): {outcome[9002]}")
            print(f"  deadlocks detected: {locks.snapshot().stats.deadlocks}")
            print("\n  The youngest loses, because it has done the least work.")
            print("  And the VICTIM is the one that fails, not whoever noticed -")
            print("  otherwise the loser would be chosen by scheduling.")

            # -- 7 ---------------------------------------------------------
            rule("7. What this costs, and where it stops")

            floor = median_ns(lambda: None)
            from engine.concurrency.snapshot import Snapshot, visible
            from engine.serialization.record import TupleHeader, read_tuple_header

            header = TupleHeader(xmin=5, xmax=0)
            snapshot = Snapshot(xmin=10, xmax=20, active=frozenset({12}), frozen_xid=3)
            raw = db._catalog.heap_for("t").get(rid_of(db, 0))

            print(
                f"  read_tuple_header   {median_ns(lambda: read_tuple_header(raw)) - floor:>5.0f} ns"
            )
            print(
                f"  visible()           {median_ns(lambda: visible(header, snapshot)) - floor:>5.0f} ns"
            )
            print("\n  The header is read BEFORE the row is decoded, so an invisible")
            print("  version costs an unpack rather than a walk of every column.")
            print()
            print("  Where it stops:")
            print("    - not serializable. Write skew is possible; ruling it out")
            print("      needs predicate locking or PostgreSQL's SSI.")
            print("    - no UPDATE, so a version chain is one deep.")
            print("    - statements do not run at the same INSTANT. Several")
            print("      transactions are open at once and genuinely conflict;")
            print("      the engine still runs one statement at a time.")
            print("    - no lock escalation, no autovacuum, and an index entry")
            print("      says 'a version of this key lives here', not a visible")
            print("      one - which is why PostgreSQL needs a visibility map.")

    print(f"\n{'-' * 78}")
    print("docs/milestone-10-mvcc.md has the full reasoning.")
    print("That is all ten milestones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
