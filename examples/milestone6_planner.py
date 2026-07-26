#!/usr/bin/env python3
"""A narrated tour of the Milestone 6 planner.

    python examples/milestone6_planner.py

Six things: what statistics say about a table, how a predicate becomes a row
count, how a row count becomes a cost, how the cheapest plan is picked, what the
losers cost, and what happens when the numbers behind all of it go stale.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.executor.binder import bind_select
from engine.executor.engine import execute_script, plan_query
from engine.optimizer.cost import (
    CPU_PREDICATE_COST,
    CPU_TUPLE_COST,
    PAGE_HIT_COST,
    PAGE_MISS_COST,
    estimate_selectivity,
)
from engine.optimizer.rules import RULES
from engine.parser.parser import parse_statement
from engine.planner.logical import describe_logical
from engine.planner.physical import PlannerOptions, describe_physical

USERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("bucket", DataType.INTEGER),
    Column("email", DataType.TEXT, nullable=False),
)

ROW_COUNT = 4_000
BUCKETS = 100
PAGE_SIZE = 512


def rule(title: str) -> None:
    print(f"\n{'-' * 78}\n{title}\n")


def main() -> int:
    print("ChenDB Milestone 6 - the cost-based planner")

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "shop.chendb"
        sink = RingBufferSink(capacity=200_000)
        tracer = Tracer(sink, TraceLevel.OPERATOR)

        with Database.open(path, page_size=PAGE_SIZE, tracer=tracer) as db:
            db.create_table("users", USERS)
            db.insert_many(
                "users",
                [
                    (n, None if n % 11 == 0 else n % BUCKETS, f"u{n:05d}@example.com")
                    for n in range(ROW_COUNT)
                ],
            )
            db.create_index("users_bucket", "users", "bucket")

            # -- 1. statistics ---------------------------------------------
            rule("1. What ANALYZE learns about a table")

            stats = db.statistics.gather("users")
            print(f"   users: {stats.row_count} rows over {stats.page_count} pages")
            print(f"   {'column':<10}{'distinct':>10}{'nulls':>8}  {'min':<16}{'max':<16}")
            for column in stats.columns:
                low = "-" if column.minimum is None else str(column.minimum)[:15]
                high = "-" if column.maximum is None else str(column.maximum)[:15]
                print(
                    f"   {column.name:<10}{column.distinct_count:>10}"
                    f"{column.null_count:>8}  {low:<16}{high:<16}"
                )
            print("\n   Four numbers per column. No histogram, which is the biggest")
            print("   source of error: 1/distinct assumes every value is equally")
            print("   common, so a column where 90% of rows share one value would be")
            print("   estimated as highly selective and chosen catastrophically.")

            # -- 2. selectivity --------------------------------------------
            rule("2. How a predicate becomes a row count")

            print(f"   {'predicate':<34}{'selectivity':>13}{'rows':>9}   how")
            for where, how in (
                ("bucket = 5", "1 / distinct"),
                ("bucket < 25", "interpolated min..max"),
                ("bucket < 75", "interpolated min..max"),
                ("bucket IS NULL", "the null fraction"),
                ("bucket <> 5", "1 - 1/distinct"),
                ("bucket < 25 AND id < 100", "product (independence)"),
                ("bucket = 5 OR bucket = 6", "inclusion-exclusion"),
                ("bucket < id", "no idea; a fixed guess"),
            ):
                statement = parse_statement(f"SELECT id FROM users WHERE {where}")
                bound = bind_select(statement, db.catalog)
                selectivity = estimate_selectivity(bound.where, stats)
                print(
                    f"   {where:<34}{selectivity:>13.4f}"
                    f"{selectivity * stats.row_count:>9.0f}   {how}"
                )
            print("\n   AND multiplies, which assumes the columns are independent.")
            print("   That is the most consequential wrong assumption in any planner:")
            print("   `city = 'Paris' AND country = 'France'` is estimated as two")
            print("   small numbers multiplied, when the second is implied by the first.")

            # -- 3. the constants ------------------------------------------
            rule("3. What things cost, measured for this engine")

            print(f"   PAGE_MISS_COST      {PAGE_MISS_COST:>6}   a pread the buffer pool could not serve")
            print(f"   PAGE_HIT_COST       {PAGE_HIT_COST:>6}   the same page, already in a frame")
            print(f"   CPU_TUPLE_COST      {CPU_TUPLE_COST:>6}   decode one record, in Python")
            print(f"   CPU_PREDICATE_COST  {CPU_PREDICATE_COST:>6}   evaluate a predicate on a decoded row")
            print("\n   PostgreSQL's defaults put cpu_tuple_cost at 1/100th of a page")
            print("   read. Here it is 1/7th, because a page read hits the OS cache")
            print("   and a row costs interpreted Python. Copying the ratio would")
            print("   have made the model refuse the index at 0.3% selectivity,")
            print("   where it still wins by 80x.")

            # -- 4. the pipeline -------------------------------------------
            rule("4. Logical, rewritten, then physical")

            statement = parse_statement(
                "SELECT id FROM users WHERE bucket > 2 * 2 AND bucket < 10"
            )
            bound = bind_select(statement, db.catalog)
            planned = plan_query(bound, db, tracer=tracer)

            print("   logical plan (no opinion on how):")
            print("     " + describe_logical(planned.logical).replace("\n", "\n     "))
            print(f"\n   rewrites that fired: {', '.join(planned.rewrites) or 'none'}")
            print("   available rules:")
            for entry in RULES:
                fired = "*" if entry.name in planned.rewrites else " "
                print(f"    {fired} {entry.name:<26} {entry.description}")
            print("\n   physical plan (an algorithm, and what it will cost):")
            print("     " + describe_physical(planned.root).replace("\n", "\n     "))
            print("\n   `2 * 2` became `4` at plan time, not once per row - and that")
            print("   is also what makes the comparison matchable by the index")
            print("   planner, which only recognises `column <op> literal`.")

            # -- 5. the choice ---------------------------------------------
            rule("5. Where the index stops paying")

            print(f"   {'predicate':<26}{'est rows':>10}{'chose':>14}   why")
            for cutoff in (1, 5, 20, 60, 95):
                sql = f"SELECT id FROM users WHERE bucket < {cutoff}"
                statement = parse_statement(sql)
                bound = bind_select(statement, db.catalog)
                planned = plan_query(bound, db, tracer=tracer)
                chosen = next(a for a in planned.alternatives if a.chosen)
                loser = next(a for a in planned.alternatives if not a.chosen)
                path = "index scan" if "Index" in chosen.access_path else "seq scan"
                # The plan root's row count, not the leaf's: a sequential scan
                # emits every row and lets a Filter above it do the work, so the
                # leaf's figure would say 4000 for every predicate.
                print(
                    f"   bucket < {cutoff:<17}{planned.estimated_rows:>10.0f}{path:>14}"
                    f"   {loser.rejected_because}"
                )
            print("\n   Milestone 5 chose the index on every one of these rows.")
            print("   benchmarks/index_vs_scan.py measures what that cost.")

            # -- 6. the alternatives ---------------------------------------
            rule("6. What the planner turned down")

            for row in execute_script(
                "EXPLAIN SELECT id FROM users WHERE bucket < 60", db, tracer=tracer
            )[0].rows:
                print(f"   {row[0]}")
            print("\n   A planner that reports only its answer cannot be argued with.")

            print("\n   Forcing the loser, to check the claim:")
            for options, label in (
                (PlannerOptions(enable_index_scan=False), "seq scan"),
                (PlannerOptions(enable_seq_scan=False), "index scan"),
            ):
                result = execute_script(
                    "SELECT id FROM users WHERE bucket < 60",
                    db,
                    tracer=tracer,
                    planner_options=options,
                )[0]
                print(
                    f"     {label:<12} {result.stats.rows_returned:>6} rows   "
                    f"{result.stats.pages_read:>6} pages   "
                    f"{result.stats.duration_ns / 1e6:>7.1f} ms"
                )
            print("   Same rows, different amount of work. A disabled path is")
            print("   penalised rather than removed, so a plan always exists -")
            print("   PostgreSQL's disable_cost does exactly the same.")

            # -- 7. staleness ----------------------------------------------
            rule("7. When the numbers go stale")

            print(f"   before: {db.statistics.for_table('users').row_count} rows, "
                  f"stale={db.statistics.is_stale('users')}")
            db.insert_many(
                "users", [(90_000 + n, 5, f"late{n}@x.com") for n in range(2000)]
            )
            after = db.statistics.for_table("users")
            print(f"   after 2000 inserts: statistics still say {after.row_count} rows, "
                  f"stale={db.statistics.is_stale('users')}")
            print("\n   Stale statistics are still used - a slightly old estimate beats")
            print("   none, and recomputing per insert would cost a full scan per row.")
            print("   They are reported instead:")
            for row in execute_script(
                "EXPLAIN SELECT id FROM users WHERE bucket = 5", db, tracer=tracer
            )[0].rows:
                if "Statistics" in str(row[0]):
                    print(f"     {row[0]}")
            print(f"\n   {execute_script('ANALYZE users', db, tracer=tracer)[0].message}")
            print(f"   stale now: {db.statistics.is_stale('users')}")

            # -- events -----------------------------------------------------
            rule("8. What the planner reported while doing all that")

            counts: dict[str, int] = {}
            for item in sink.snapshot():
                if item.category == "planner":
                    counts[item.event_type] = counts.get(item.event_type, 0) + 1
            for name, count in sorted(counts.items(), key=lambda e: -e[1]):
                print(f"   {name:<24}{count:>6}")
            print("\n   One PlanAlternativeEvent per candidate, chosen or not, so the")
            print("   decision is auditable from the event stream alone.")

    print("\n" + "-" * 78)
    print("Try it in the browser: python -m engine.server, then the Execution tab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
