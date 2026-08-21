#!/usr/bin/env python3
"""Measure a join as the tables grow, and what the other algorithm would cost.

    python benchmarks/joins.py

Two questions the planner has to answer for every join, and this prices both:

1. **Hash or nested loop?** A hash join builds a table on one side and probes it
   with the other, so it costs one pass over each. A nested loop rescans the
   inner side per outer row, so it costs the *product*. The crossover is not
   interesting because there is not one: the loop is the algorithm of last
   resort, kept for predicates that cannot be hashed. What is interesting is how
   fast the gap opens, and that is what the table below shows.

2. **Which side builds?** Memory is proportional to the build side, so the
   planner puts the smaller estimate there. This reports the side it chose and
   the row count it based that on, at every size.

``PlannerOptions`` has no switch for the join algorithm, only for access paths,
so the loop is forced the honest way: plan the query, replace the hash node with
a nested-loop node over the same inputs and predicate, and run that plan. Same
inputs, same output, one different algorithm.

Absolute times depend on the machine. The ratio between the two algorithms, and
the way it grows with cardinality, do not.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.executor.binder import bind_select
from engine.executor.engine import build_logical_plan, materialise
from engine.executor.operators import ExecutionContext, Operator
from engine.parser.parser import parse
from engine.planner.physical import (
    PhysicalHashJoin,
    PhysicalNestedLoopJoin,
    PhysicalNode,
    plan_select,
)

#: (customers, orders). Every order points at a customer, so the join is a
#: foreign-key join and its output is exactly the order count.
SIZES = ((100, 400), (400, 1600), (1600, 6400))

#: A nested loop over these is `left * right` row comparisons in interpreted
#: Python. Past this product the forced run is skipped and *said* to be skipped,
#: because a benchmark that quietly drops a case reads as one that ran it.
NESTED_LOOP_CEILING = 3_000_000

PAGE_SIZE = 4096
REPEATS = 3

JOIN_SQL = "SELECT c.name, o.total FROM customers c JOIN orders o ON c.id = o.customer_id"

CUSTOMERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
)
ORDERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("customer_id", DataType.INTEGER, nullable=False),
    Column("total", DataType.INTEGER, nullable=False),
)


def build(path: Path, customers: int, orders: int) -> Database:
    db = Database.open(path, page_size=PAGE_SIZE)
    db.create_table("customers", CUSTOMERS)
    db.create_table("orders", ORDERS)
    db.insert_many("customers", [(n, f"customer-{n:06d}") for n in range(customers)])
    db.insert_many(
        "orders",
        [(n, n % customers, (n * 37) % 5000) for n in range(orders)],
    )
    db.analyze()
    db.sync()
    return db


def planned_root(db: Database, sql: str) -> PhysicalNode:
    """Parse, bind and plan ``sql``, stopping before anything runs."""
    statement = parse(sql)[0]
    bound = bind_select(statement, db.catalog)
    return plan_select(build_logical_plan(bound), db).root


def force_nested_loop(node: PhysicalNode) -> PhysicalNode:
    """Return ``node`` with every hash join replaced by a nested loop.

    The nodes are frozen dataclasses, so this rebuilds the spine rather than
    mutating it. ``PhysicalNestedLoopJoin`` carries a subset of the hash join's
    fields (it has no build key, probe key or residual, because it evaluates the
    whole predicate per pair) so the copy is field-by-field over the subset.
    """
    replacements = {
        field.name: force_nested_loop(getattr(node, field.name))
        for field in fields(node)
        if isinstance(getattr(node, field.name), PhysicalNode)
    }
    if isinstance(node, PhysicalHashJoin):
        wanted = {field.name for field in fields(PhysicalNestedLoopJoin)}
        carried = {name: getattr(node, name) for name in wanted if hasattr(node, name)}
        return PhysicalNestedLoopJoin(**{**carried, **replacements})
    if not replacements:
        return node
    kept = {field.name: getattr(node, field.name) for field in fields(node)}
    return type(node)(**{**kept, **replacements})


def drain(root: PhysicalNode, db: Database) -> int:
    """Run a physical plan to exhaustion. Returns the row count."""
    context = ExecutionContext(max_rows=None, snapshot=db.snapshot())
    operator: Operator = materialise(root, db, context)
    rows = 0
    operator.open()
    try:
        while operator.next() is not None:
            rows += 1
    finally:
        operator.close()
    return rows


def timed(fn) -> tuple[float, int]:
    """Median wall time in milliseconds, plus the last run's row count."""
    samples = []
    rows = 0
    for _ in range(REPEATS):
        started = time.perf_counter_ns()
        rows = fn()
        samples.append((time.perf_counter_ns() - started) / 1e6)
    return statistics.median(samples), rows


def find_join(node: PhysicalNode) -> PhysicalNode | None:
    if isinstance(node, PhysicalHashJoin | PhysicalNestedLoopJoin):
        return node
    for child in node.children:
        found = find_join(child)
        if found is not None:
            return found
    return None


def header(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def main() -> int:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        print(
            f"ChenDB join benchmark - {PAGE_SIZE}-byte pages, medians of "
            f"{REPEATS} runs\n"
            f"Query: {JOIN_SQL}"
        )

        header("Hash join against the nested loop it replaced")
        print(
            f"  {'customers x orders':<22}{'rows out':>9}{'hash':>10}"
            f"{'nested loop':>14}{'ratio':>8}"
        )
        for customers, orders in SIZES:
            db = build(root / f"j{customers}.chendb", customers, orders)
            plan = planned_root(db, JOIN_SQL)
            hash_ms, rows = timed(lambda plan=plan, db=db: drain(plan, db))
            product = customers * orders
            if product <= NESTED_LOOP_CEILING:
                loop_plan = force_nested_loop(plan)
                loop_ms, _ = timed(lambda p=loop_plan, db=db: drain(p, db))
                loop = f"{loop_ms:9.1f} ms"
                ratio = f"{loop_ms / hash_ms:6.1f}x"
            else:
                loop = "  not run"
                ratio = f"{product / 1e6:5.1f}M pairs"
            print(
                f"  {f'{customers:,} x {orders:,}':<22}{rows:>9,}"
                f"{hash_ms:>7.1f} ms{loop:>14}{ratio:>8}"
            )
            db.close()
        print(
            f"\n  The loop is skipped above {NESTED_LOOP_CEILING / 1e6:.0f}M row "
            "comparisons, which is a limit\n"
            "  of this benchmark's patience rather than of the operator: the\n"
            "  engine will still run it if a predicate leaves it no choice,\n"
            "  which is exactly why it is kept."
        )

        header("Which side builds, and what the estimate said")
        print(
            f"  {'customers x orders':<22}{'build side':>12}{'est. rows':>11}"
            f"{'probe side':>12}{'est. rows':>11}"
        )
        for customers, orders in SIZES:
            db = build(root / f"b{customers}.chendb", customers, orders)
            join = find_join(planned_root(db, JOIN_SQL))
            assert join is not None, "the plan has no join"
            build_side, probe_side = join.left, join.right
            print(
                f"  {f'{customers:,} x {orders:,}':<22}"
                f"{_table_of(build_side):>12}{build_side.estimated.rows:>11,.0f}"
                f"{_table_of(probe_side):>12}{probe_side.estimated.rows:>11,.0f}"
            )
            db.close()
        print(
            "\n  The build side is the physical left, and the planner puts the\n"
            "  smaller estimate there because the hash table it holds is what\n"
            "  costs memory. Getting this backwards is the difference between a\n"
            "  hash table of a thousand rows and one of a million."
        )

        header("Estimated against actual, at the join")
        print(f"  {'customers x orders':<22}{'estimated':>11}{'actual':>10}{'error':>9}")
        for customers, orders in SIZES:
            db = build(root / f"e{customers}.chendb", customers, orders)
            plan = planned_root(db, JOIN_SQL)
            join = find_join(plan)
            assert join is not None, "the plan has no join"
            actual = drain(plan, db)
            estimated = join.estimated.rows
            error = estimated / actual if actual else 0.0
            print(
                f"  {f'{customers:,} x {orders:,}':<22}{estimated:>11,.0f}"
                f"{actual:>10,}{error:>8.2f}x"
            )
            db.close()
        print(
            "\n  A foreign-key join is the case an estimator should get right:\n"
            "  every order matches exactly one customer, so the answer is the\n"
            "  order count. An error here is an error in the most favourable\n"
            "  case there is, which is why it is worth printing every time."
        )

    return 0


def _table_of(node: PhysicalNode) -> str:
    """The table a plan subtree scans, for labelling a build side."""
    name = getattr(node, "table_name", None)
    if isinstance(name, str):
        return name[:11]
    for child in node.children:
        found = _table_of(child)
        if found != "?":
            return found
    return "?"


if __name__ == "__main__":
    raise SystemExit(main())
