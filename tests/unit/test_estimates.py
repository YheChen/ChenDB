"""Do the planner's estimates match reality?

Nothing in this project asked that until Milestone 20, and the gap is exactly
how ``join_selectivity`` divided by row counts instead of distinct counts for
fourteen milestones. Every test around it checked that the arithmetic was the
arithmetic somebody had written down. None checked the answer against the number
of rows the query actually returns, so an estimate 80x under its true value
passed every one of them.

This file is that check. Each case runs twice: once for what the planner
predicted, once for what came back. A bad estimate never produces a wrong
answer, so this cannot fail loudly on its own; what it can do is refuse to let a
regression through quietly, which is the failure mode the cost model has.

**The tolerances are the interesting part.** An estimate is not expected to be
right, it is expected to be right enough to order two plans correctly, so most
cases allow a factor of two either way. The ones marked exact are exact for a
reason worth knowing: when a column has no more distinct values than
:data:`STATISTICS_TARGET`, its most-common list is the whole column, and the
planner is counting rather than estimating.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.executor.engine import execute_script

EVENTS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("level", DataType.TEXT),
    Column("code", DataType.INTEGER),
    Column("note", DataType.TEXT),
)
ORDERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("user_id", DataType.INTEGER),
)
SHIPMENTS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("user_id", DataType.INTEGER),
)
USERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
)

ROWS = 4_000
USER_COUNT = 50


def level_of(position: int) -> str:
    """90% info, 9% warn, 1% error. What ``1 / distinct`` cannot see."""
    if position % 100 == 0:
        return "error"
    if position % 100 < 10:
        return "warn"
    return "info"


def code_of(position: int) -> int:
    """90% of rows in 0..9, the rest spread to 999.

    A straight line from min to max says nothing true about this column, which
    is the case the histogram exists for.
    """
    if position % 10 or position % 100:
        return position % 10
    return (position // 4) % 1000


def busy_user(position: int) -> int:
    """60% of rows belong to user 1. A join key with a hot value in it."""
    return 1 if position % 10 < 6 else 2 + (position % (USER_COUNT - 1))


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """Deliberately skewed, and analyzed once for the whole module.

    Uniform data is the case every estimator gets right. Nothing here is
    uniform except ``users.id``, which is there so the suite also covers the
    join shape that was wrong by 80x before Milestone 19.
    """
    path = tmp_path_factory.mktemp("estimates") / "skew.chendb"
    with Database.open(path, page_size=4096) as handle:
        handle.create_table("events", EVENTS)
        handle.create_table("orders", ORDERS)
        handle.create_table("shipments", SHIPMENTS)
        handle.create_table("users", USERS)
        handle.insert_many(
            "events",
            [
                (n, level_of(n), code_of(n), None if n % 4 == 0 else f"n{n}")
                for n in range(ROWS)
            ],
        )
        handle.insert_many("orders", [(n, busy_user(n)) for n in range(ROWS)])
        handle.insert_many("shipments", [(n, busy_user(n * 3)) for n in range(ROWS)])
        handle.insert_many("users", [(n, f"u{n}") for n in range(USER_COUNT)])
        handle.analyze()
        yield handle


@dataclass(frozen=True, slots=True)
class Case:
    """One query, and how close the estimate has to be."""

    source: str
    """Everything after ``FROM``, so the same text can be counted and explained."""
    factor: float = 2.0
    """The estimate must be within this ratio of the true count, either way."""
    exact: bool = False
    why: str = ""

    @property
    def id(self) -> str:
        return self.source


CASES = (
    # -- categorical skew: three values, 90/9/1 ------------------------------
    Case("events WHERE level = 'info'", exact=True, why="the hot value"),
    Case("events WHERE level = 'warn'", exact=True),
    Case(
        "events WHERE level = 'error'",
        exact=True,
        why="1% of the table. `1 / distinct` said 33% and would pick a scan",
    ),
    Case("events WHERE level <> 'info'", exact=True),
    Case("events WHERE level IS NULL", exact=True),
    # -- numeric skew: the histogram's job -----------------------------------
    Case("events WHERE code = 3", exact=True),
    Case("events WHERE code < 10", factor=1.2),
    Case("events WHERE code > 500", factor=3.0, why="the sparse tail, 19 rows"),
    Case("events WHERE code >= 0", exact=True),
    Case("events WHERE code < 0", exact=True, why="empty, and floored at one row"),
    # -- NULLs ---------------------------------------------------------------
    Case("events WHERE note IS NULL", exact=True),
    Case("events WHERE note IS NOT NULL", exact=True),
    # -- conjunctions, where independence is assumed and is wrong ------------
    Case("events WHERE level = 'warn' AND code < 10", factor=6.0),
    Case("events WHERE level = 'error' OR level = 'warn'", factor=2.0),
    # -- joins ---------------------------------------------------------------
    Case(
        "users u JOIN orders o ON u.id = o.user_id",
        factor=1.2,
        why="a foreign key. Estimated at 50 against 4,000 before Milestone 19",
    ),
    Case(
        "orders o JOIN shipments s ON o.user_id = s.user_id",
        factor=1.2,
        why="many to many with a hot key. 18x low on distinct counts alone",
    ),
    Case("users u LEFT JOIN orders o ON u.id = o.user_id", factor=1.2),
    Case(
        "orders o JOIN shipments s ON o.user_id = s.user_id AND o.id = s.id",
        factor=15.0,
        why="two conditions, multiplied as if independent. They are not",
    ),
)


def estimate_and_actual(db: Database, source: str) -> tuple[float, int]:
    """What the planner predicted, and what the query returned.

    Counted with ``COUNT(*)`` rather than by taking ``len(rows)``, because
    ``execute_script`` stops at ``DEFAULT_MAX_ROWS``. The skewed join here
    produces 5.8 million rows, so measuring the length of the result would have
    quietly compared the estimate against 10,000 and called it a 581x error.
    """
    planned = execute_script(f"EXPLAIN SELECT * FROM {source}", db)[-1].planned
    assert planned is not None
    counted = execute_script(f"SELECT COUNT(*) FROM {source}", db)[-1].rows
    return planned.estimated_rows, counted[0][0]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_estimate_is_close_to_the_truth(db: Database, case: Case):
    estimated, actual = estimate_and_actual(db, case.source)
    note = f" ({case.why})" if case.why else ""

    if case.exact:
        # A floor of one row, not zero: an absent value may simply be newer than
        # the last ANALYZE, and an estimate of zero makes everything above the
        # scan look free.
        assert estimated == pytest.approx(max(actual, 1)), (
            f"SELECT * FROM {case.source}{note}\n"
            f"  estimated {estimated}, actual {actual}. This case is exact "
            f"because the column's most-common list covers every value, so a "
            f"mismatch means the list stopped being complete or stopped being read."
        )
        return

    floor = max(actual, 1)
    ratio = estimated / floor
    assert 1 / case.factor <= ratio <= case.factor, (
        f"SELECT * FROM {case.source}{note}\n"
        f"  estimated {estimated:,.0f}, actual {actual:,} ({ratio:.2f}x), "
        f"outside the allowed {case.factor}x"
    )


def test_the_suite_covers_a_join_that_skew_breaks(db: Database):
    """The case this file exists for, asserted rather than left in the table.

    Both sides of this join put 60% of their rows on one user. The true size is
    dominated by that value alone, 2,400 by 2,400, and `1 / max(distinct)` has
    no way to know: it sees fifty distinct values on each side and predicts
    4000 * 4000 / 50, which is 320,000 against a real 5.8 million.

    An 18x underestimate is not a rounding error. It is the difference between
    a hash join and a nested loop, and every operator above it is costed as if
    the join produced a twentieth of what it does.
    """
    estimated, actual = estimate_and_actual(
        db, "orders o JOIN shipments s ON o.user_id = s.user_id"
    )
    assert actual > 5_000_000, "the fixture stopped being skewed"

    orders = db.statistics.for_table("orders").column(1)
    shipments = db.statistics.for_table("shipments").column(1)
    without_skew = ROWS * ROWS / max(orders.distinct_count, shipments.distinct_count)
    assert without_skew < actual / 10, "the old estimate should be far too low"
    assert 0.8 <= estimated / actual <= 1.25


def test_an_unanalyzed_table_still_gets_an_estimate(tmp_path: Path):
    """Statistics are gathered lazily, so there is no such thing as no estimate.

    Worth pinning next to the accuracy cases: the fallbacks they never reach are
    reached constantly by a freshly opened database, and a change to the summary
    that happens to crash on an empty one would go unnoticed here otherwise.
    """
    with Database.open(tmp_path / "cold.chendb", page_size=4096) as db:
        db.create_table("events", EVENTS)
        estimated, actual = estimate_and_actual(db, "events WHERE code < 10")
        assert actual == 0
        assert estimated >= 0

        db.insert_many("events", [(n, level_of(n), code_of(n), None) for n in range(64)])
        # Still no ANALYZE. The statistics are stale, not absent, and the
        # planner uses them anyway rather than refusing to plan.
        estimated, actual = estimate_and_actual(db, "events WHERE code < 10")
        assert actual == 64
        assert estimated >= 0
