#!/usr/bin/env python3
"""A narrated tour of Milestone 13: joins, aggregation, and a planner with a job.

    python examples/milestone13_joins.py

Six things: what a join costs when you get the algorithm wrong, why the row
layout never moves however the tables are reordered, how the search over join
orders actually works, where predicate pushdown earns its keep, what aggregation
does to a pipeline, and where the whole thing stops.

Twelve milestones in, the planner chose between two ways to read one table. This
is the first time it has had a decision it could plausibly get wrong.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.executor.engine import execute_script
from engine.optimizer.cost import hash_join_cost, nested_loop_join_cost
from engine.planner.physical import (
    MAX_TABLES_TO_ENUMERATE,
    PhysicalHashJoin,
    PhysicalNestedLoopJoin,
    walk_physical,
)

CUSTOMERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
    Column("city", DataType.TEXT),
)
SALES = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("customer_id", DataType.INTEGER),
    Column("amount", DataType.INTEGER),
)
LINES = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("sale_id", DataType.INTEGER),
    Column("sku", DataType.TEXT),
)
PAGE_SIZE = 4096
CITIES = ["london", "new york", "amsterdam", "tokyo"]


def rule(title: str) -> None:
    print(f"\n{'-' * 78}\n{title}\n")


def run(db: Database, sql: str):
    return execute_script(sql, db)[-1]


def explain(db: Database, sql: str) -> None:
    for row in run(db, f"EXPLAIN {sql}").rows:
        print(f"    {row[0]}")


def main() -> int:
    print(__doc__)

    with (
        tempfile.TemporaryDirectory() as directory,
        Database.open(Path(directory) / "shop.chendb", page_size=PAGE_SIZE) as db,
    ):
        db.create_table("customers", CUSTOMERS)
        db.create_table("sales", SALES)
        db.create_table("lines", LINES)
        db.insert_many(
            "customers",
            [(n, f"c{n}", CITIES[n % len(CITIES)]) for n in range(40)],
        )
        db.insert_many("sales", [(n, n % 40, 10 + (n * 7) % 500) for n in range(800)])
        db.insert_many("lines", [(n, n % 800, f"sku{n % 25}") for n in range(1600)])
        db.analyze()

        # -- 1 ---------------------------------------------------------
        rule("1. Two algorithms, three orders of magnitude apart")

        print("  40 customers joined to 800 sales, as the cost model prices it:")
        nested = nested_loop_join_cost(40, 800, matches=800)
        hashed = hash_join_cost(40, 800, matches=800)
        print(f"    nested loop   {nested.total:>10.1f}   O(n x m) comparisons")
        print(f"    hash join     {hashed.total:>10.1f}   O(n + m)")
        print(f"    ratio         {nested.total / hashed.total:>10.0f}x")

        join_sql = (
            "SELECT c.name, s.amount FROM customers c JOIN sales s ON c.id = s.customer_id"
        )
        planned = run(db, f"EXPLAIN {join_sql}").planned
        chosen = next(
            node
            for node in walk_physical(planned.root)
            if isinstance(node, PhysicalHashJoin | PhysicalNestedLoopJoin)
        )
        print(f"\n  Chosen: {chosen.node_type}")
        print(f"  Build side: {chosen.left.table_name}  (the smaller one)")
        print("\n  The build side is not a rule. A hash-table insert costs 67 ns")
        print("  and a probe 45 ns, both measured, so putting the bigger side")
        print("  on the build is simply more expensive. The planner arrives at")
        print("  'build on the small side' by arithmetic.")

        # -- 2 ---------------------------------------------------------
        rule("2. A range join has no key, so it falls back")

        explain(
            db,
            "SELECT c.id FROM customers c JOIN sales s ON c.id < s.amount",
        )
        print("\n  Nothing to hash. This is why a range join is slow in every")
        print("  engine and not just this one, and the plan says so instead of")
        print("  quietly being quadratic.")

        # -- 3 ---------------------------------------------------------
        rule("3. The row layout never moves")

        print("  FROM customers c JOIN sales s  lays the joined row out as")
        print("    [ c.id c.name c.city | s.id s.customer_id s.amount ]")
        print("       0     1      2        3       4            5")
        print()
        print("  ...and it stays that way whichever order the planner joins in.")
        print("  A bound column index is computed once, by the binder, against")
        print("  the order the tables were WRITTEN in. Below the top join every")
        print("  row is full width with the not-yet-joined tables left empty.")
        print()
        print("  The alternative is remapping indices at every level, and the")
        print("  cost of that is a mapping the whole executor has to carry.")
        print("  This trades width for never needing it.")

        # -- 4 ---------------------------------------------------------
        rule("4. Choosing an order, and how big that search is")

        three = (
            "SELECT c.name, l.sku FROM customers c "
            "JOIN sales s ON c.id = s.customer_id "
            "JOIN lines l ON s.id = l.sale_id "
            "WHERE c.city = 'london'"
        )
        explain(db, three)
        print()
        print("  System R's dynamic programme: solve every one-table set, build")
        print("  every two-table set from those, and so on. The best plan for")
        print("  {a,b,c} uses the best plan for one of its subsets, so each")
        print("  subset is solved once instead of re-derived down every branch.")
        print()
        print("    tables   left-deep orders   DP subsets")
        for n in (3, 5, 8, 12):
            orders = 1
            for k in range(2, n + 1):
                orders *= k
            print(f"    {n:>6}   {orders:>16,}   {3**n:>10,}")
        print()
        print(f"  ChenDB enumerates up to {MAX_TABLES_TO_ENUMERATE} tables and goes greedy")
        print("  above that, and SAYS which it did. PostgreSQL's threshold is 12")
        print("  and it switches to a genetic algorithm.")

        # -- 5 ---------------------------------------------------------
        rule("5. Pushdown is a rewrite, not a choice")

        print("  Look again at the plan above: `city = 'london'` is BELOW the")
        print("  join, not above it. It shrinks the input to every join over it,")
        print("  and it can never be worse there, which is exactly what makes")
        print("  it a rewrite rather than a costed alternative.")
        print()
        print("  It is also what makes an index reachable at all: a predicate")
        print("  left above a join is applied to join output, where no index")
        print("  exists. Pushed to the scan it becomes an access-path decision.")
        db.create_index("customers_city", "customers", "city")
        db.analyze()
        explain(db, "SELECT c.name FROM customers c WHERE c.city = 'london'")

        # -- 6 ---------------------------------------------------------
        rule("6. Aggregation stops the pipeline")

        grouped = (
            "SELECT c.city, COUNT(*) AS orders, SUM(s.amount) AS revenue "
            "FROM customers c JOIN sales s ON c.id = s.customer_id "
            "GROUP BY c.city HAVING SUM(s.amount) > 1000 "
            "ORDER BY revenue DESC LIMIT 3"
        )
        result = run(db, grouped)
        print("  " + " | ".join(column.name for column in result.columns))
        for row in result.rows:
            print("  " + " | ".join(str(value) for value in row))
        print()
        print("  Every input row is read before the first output row exists,")
        print("  because a group is not complete until the input is. So is a")
        print("  sort. That is why LIMIT 3 over this saves nothing at all, ")
        print("  and the plan shows it, because the child's cost does not fall.")

        started = time.perf_counter_ns()
        run(db, "SELECT COUNT(*) FROM sales")
        scalar_ns = time.perf_counter_ns() - started
        started = time.perf_counter_ns()
        run(db, "SELECT customer_id, COUNT(*) FROM sales GROUP BY customer_id")
        grouped_ns = time.perf_counter_ns() - started
        print()
        print(f"    COUNT(*) over 800 rows        {scalar_ns / 1000:>8.0f} us")
        print(f"    the same, grouped 40 ways     {grouped_ns / 1000:>8.0f} us")
        print("  Hashing is linear in rows and independent of the group count,")
        print("  which is why grouping is nearly free once the rows are read.")

        print("\n  Where it stops:")
        print("    - INNER joins only. An outer join fixes which side may be")
        print("      the outer relation, so the planner could no longer reorder")
        print("      freely, and the executor would have to NULL-extend.")
        print("    - No index nested-loop join. With an index on the inner side")
        print("      and a tiny outer side that beats a hash join, and ChenDB")
        print("      never considers it.")
        print("    - Left-deep plans only. Joining (a x b) to (c x d) is")
        print("      sometimes better; System R excluded bushy plans in 1979 and")
        print("      most optimizers still do.")
        print("    - Join cardinality assumes no skew. Ten million orders over")
        print("      three customers is still distinct=3, and the estimate is")
        print("      off by six orders of magnitude. A most-common-values list")
        print("      is the biggest single thing missing from the cost model.")
        print("    - Sorts and hash tables are memory-only. No spill to disk.")
        print("    - No DISTINCT, no subqueries, no window functions.")

    print(f"\n{'-' * 78}")
    print("docs/milestone-13-joins.md has the full reasoning.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
