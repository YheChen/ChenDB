#!/usr/bin/env python3
"""A narrated tour of Milestone 20: statistics that survive skew.

    python examples/milestone20_skew.py

Three queries against one table, returning 18,000 rows, 1,800 rows and 200 rows.
Before this milestone the planner estimated the same number for all three, cost
them identically, and chose the same plan, because `1 / distinct` says every
value is equally common.

Five things: what uniformity costs, the two structures that replace it, the
regime where an estimate stops being an estimate, joins where the error
compounds, and how any of this is checked at all.
"""

from __future__ import annotations

import statistics as pystats
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.executor.engine import execute_script
from engine.planner import statistics as statistics_module
from engine.planner.physical import PhysicalIndexScan, walk_physical
from engine.planner.statistics import STATISTICS_TARGET, summarise

WIDTH = 78
ROWS = 20_000

EVENTS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("level", DataType.TEXT),
    Column("code", DataType.INTEGER),
)
ORDERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("user_id", DataType.INTEGER),
)
SHIPMENTS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("user_id", DataType.INTEGER),
)


def level_of(position: int) -> str:
    """90% info, 9% warn, 1% error. The shape a single average cannot hold."""
    if position % 100 == 0:
        return "error"
    if position % 100 < 10:
        return "warn"
    return "info"


def code_of(position: int) -> int:
    """90% of rows in 0..9, the rest spread to 999."""
    if position % 10 or position % 100:
        return position % 10
    return (position // 4) % 1000


def busy_user(position: int) -> int:
    """60% of rows belong to user 1. A join key with a hot value in it."""
    return 1 if position % 10 < 6 else 2 + (position % 49)


def rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * WIDTH}\n{title}\n{'─' * WIDTH}")
    else:
        print("─" * WIDTH)


def heading(text: str) -> None:
    print(f"\n{text}\n{'·' * len(text)}")


def run(database: Database, sql: str):
    return execute_script(sql, database)[-1]


def count(database: Database, source: str) -> int:
    return run(database, f"SELECT COUNT(*) FROM {source}").rows[0][0]


def estimate(database: Database, source: str) -> float:
    return run(database, f"EXPLAIN SELECT * FROM {source}").planned.estimated_rows


def uses_index(database: Database, source: str) -> bool:
    planned = run(database, f"EXPLAIN SELECT * FROM {source}").planned
    return any(isinstance(node, PhysicalIndexScan) for node in walk_physical(planned.root))


def timed(database: Database, sql: str, runs: int = 9) -> float:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        execute_script(sql, database)
        samples.append((time.perf_counter() - started) * 1000)
    return pystats.median(samples)


def build(directory: str, *, summarising: bool) -> Database:
    """The same data, analyzed with or without Milestone 20's summary.

    Turning the summary off is how "before" is measured rather than remembered:
    :func:`summarise` is what produces both structures, so a version that
    returns nothing puts the estimator back on ``1 / distinct`` and the straight
    line from min to max, with every other line of the engine unchanged.
    """
    statistics_module.summarise = summarise if summarising else (lambda counts: ((), ()))
    database = Database.open(Path(directory) / "skew.chendb", page_size=4096)
    database.create_table("events", EVENTS)
    database.create_table("orders", ORDERS)
    database.create_table("shipments", SHIPMENTS)
    database.insert_many("events", [(n, level_of(n), code_of(n)) for n in range(ROWS)])
    database.insert_many("orders", [(n, busy_user(n)) for n in range(4000)])
    database.insert_many("shipments", [(n, busy_user(n * 3)) for n in range(4000)])
    database.create_index("events_level", "events", "level")
    database.analyze()
    return database


def what_uniformity_costs() -> None:
    rule("1. One estimate for three queries that differ by 90x")
    print("""
`level` holds three values: 90% info, 9% warn, 1% error. `1 / distinct` divides
by three and hands back the same number whatever you asked for.
""")
    for summarising in (False, True):
        with (
            tempfile.TemporaryDirectory() as directory,
            build(directory, summarising=summarising) as database,
        ):
            label = "after " if summarising else "before"
            for value in ("info", "warn", "error"):
                source = f"events WHERE level = '{value}'"
                sql = f"SELECT COUNT(*) FROM {source}"
                path = "index scan" if uses_index(database, source) else "seq scan"
                named = f"level = '{value}'"
                print(
                    f"  {label}  {named:<16} "
                    f"estimate {estimate(database, source):7.0f}   "
                    f"actual {count(database, source):6d}   "
                    f"{path:<11} {timed(database, sql):6.1f} ms"
                )
        print()
    print("""    Three identical numbers became three right ones, and the hot value
    stopped being read through an index it should never have used. The 1.2x is
    the smaller half of that: a planner giving the same answer for 200 rows and
    18,000 is not making a decision, and everything above it inherits that.
""")


def the_two_structures() -> None:
    rule("2. Skew lives in the head, shape lives in the tail")
    print("""
So they are answered by two different structures, which is the division
PostgreSQL's pg_statistic makes for the same reason.
""")
    with (
        tempfile.TemporaryDirectory() as directory,
        build(directory, summarising=True) as database,
    ):
        events = database.statistics.for_table("events")
        level, code = events.column(1), events.column(2)

        heading("most_common: the top values, with exact counts")
        for value, rows in level.most_common:
            share = rows / ROWS * 100
            print(f"  {value!r:<10} {rows:6,d} rows  {share:5.1f}%  {'█' * int(share / 2)}")

        heading("histogram: equi-depth boundaries over everything else")
        print(
            f"  code has {code.distinct_count} distinct values, "
            f"{len(code.most_common)} of them in the list, "
            f"{max(len(code.histogram) - 1, 0)} buckets over the rest"
        )
        print(f"  bounds: {', '.join(str(bound) for bound in code.histogram[:8])} …")
        print("""
    Equi-depth, not equi-width, and that is the choice that makes a histogram
    worth having. Equal-width buckets over a column clustered at one end put
    nearly every row in one bucket and describe empty space in detail. Equal
    depth spends resolution where the rows are.
""")


def when_an_estimate_stops_being_one() -> None:
    rule("3. Where the estimate stops being an estimate")
    print(f"""
ChenDB reads every row rather than sampling, and now counts each value while it
is there. So when a column has no more than {STATISTICS_TARGET} distinct values,
the list is not a summary of the column. It is the column.
""")
    small = Counter({"info": 18_000, "warn": 1_800, "error": 200})
    listed, histogram = summarise(small)
    print(
        f"  three distinct values   complete={len(listed) == len(small)}   "
        f"histogram={histogram or 'none needed'}"
    )

    wide = Counter(dict.fromkeys(range(500), 1))
    listed, histogram = summarise(wide)
    print(
        f"  500 distinct values     complete={len(listed) == len(wide)}   "
        f"list={len(listed)}  buckets={len(histogram) - 1}"
    )
    print("""
    A complete list can say something no estimate could: that a value it has
    never seen occurs zero times. It says one row instead, and that floor is
    not timidity. Every number here is as of the last ANALYZE, so "does not
    occur" means "did not occur when we looked", and an estimate of zero makes
    every operator above the scan free. A subtree costed at nothing wins every
    comparison it is ever part of.
""")


def joins_compound() -> None:
    rule("4. A join is where the error compounds, and where it was worst")
    print("""
Two tables, 4,000 rows each, both putting 60% of their rows on user 1. The true
size of the join is dominated by that one value: 2,400 times 2,400.

`1 / max(distinct)` sees fifty distinct values on each side and cannot know.
""")
    for summarising in (False, True):
        with (
            tempfile.TemporaryDirectory() as directory,
            build(directory, summarising=summarising) as database,
        ):
            source = "orders o JOIN shipments s ON o.user_id = s.user_id"
            predicted, actual = estimate(database, source), count(database, source)
            label = "after " if summarising else "before"
            print(
                f"  {label}  estimate {predicted:12,.0f}   actual {actual:12,d}"
                f"   {predicted / actual:6.2f}x"
            )
    print("""
    An 18x underestimate is the difference between a hash join and a nested
    loop, and every operator above it is costed as if the join produced a
    twentieth of what it does. The three-term match is PostgreSQL's
    eqjoinsel_inner in miniature; when both lists are complete two of the terms
    are zero and the first is the true cardinality.
""")


def how_it_is_checked() -> None:
    rule("5. How a cost model is checked at all")
    print("""
A bad estimate never produces a wrong answer. It produces a slow query, which
looks exactly like a fast one that had more work to do, which is why an estimate
80x under its true value survived fourteen milestones of tests.

Every test around it checked that the arithmetic was the arithmetic somebody had
written down. None compared the answer to the number of rows the query actually
returns.

  tests/unit/test_estimates.py   runs each case twice, once for the prediction
                                 and once for the result, and compares them

The tolerances are the interesting part. An estimate is not expected to be
right, only right enough to order two plans correctly, so most cases allow a
factor of two. Ten of the eighteen are exact, because their column's list is
complete. Two are deliberately loose and say why in the case itself.
""")


def main() -> int:
    rule("Milestone 20: statistics that survive skew")
    print("""
`1 / distinct` assumes every value is equally common. Almost no real column is,
and this milestone is the cost model finding out.
""")
    original = statistics_module.summarise
    try:
        what_uniformity_costs()
        the_two_structures()
        when_an_estimate_stops_being_one()
        joins_compound()
        how_it_is_checked()
    finally:
        statistics_module.summarise = original

    rule("Where it stops")
    print("""
- Conjunctions are still multiplied as if independent. `city = 'Paris' AND
  country = 'France'` is the product of two small numbers when the second is
  implied by the first. PostgreSQL needed CREATE STATISTICS for this, and it is
  the largest error left in the model.
- No multi-column and no expression statistics. `WHERE lower(name) = 'ada'` has
  nothing to read.
- Statistics are still not persisted, still on purpose: a wrong one costs a slow
  query and never a wrong answer, so the file format should not change for it.
- The summary is exact rather than sampled, which is affordable at this scale
  and would not be at another.

  docs/milestone-20-skew.md has the estimator in full.
""")
    rule()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
