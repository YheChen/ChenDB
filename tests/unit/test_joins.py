"""Joins and aggregation.

Twelve milestones of ``SELECT`` meant one table, filtered and projected. The
planner chose between two access paths and the cost model, calibrated by
measurement in Milestone 6, had never had to make a decision it could plausibly
get wrong.

Joins change that twice over: there is more than one algorithm, and there is
more than one *order*. The tests are grouped accordingly (what the rows are,
then what the planner did to produce them) because the two fail for completely
different reasons and a mixed suite would not say which.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.errors import BindingError
from engine.executor.engine import execute_script
from engine.optimizer import rules
from engine.planner import physical
from engine.planner.physical import (
    PhysicalHashJoin,
    PhysicalIndexScan,
    PhysicalJoin,
    PhysicalNestedLoopJoin,
    PhysicalSeqScan,
    walk_physical,
)

USERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
    Column("city", DataType.TEXT),
)
ORDERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("user_id", DataType.INTEGER),
    Column("total", DataType.INTEGER),
)

USER_ROWS = [(1, "ada", "london"), (2, "alan", "london"), (3, "grace", "ny")]
ORDER_ROWS = [
    (10, 1, 100),
    (11, 1, 250),
    (12, 2, 80),
    (13, 3, 400),
    (14, 3, 50),
    (15, 99, 7),  # an orphan: no user 99
    (16, None, 5),  # a NULL key, which matches nothing including other NULLs
]


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "joins.chendb", page_size=4096) as handle:
        handle.create_table("users", USERS)
        handle.create_table("orders", ORDERS)
        handle.insert_many("users", USER_ROWS)
        handle.insert_many("orders", ORDER_ROWS)
        yield handle


def rows(db: Database, sql: str):
    return list(execute_script(sql, db)[-1].rows)


def names(db: Database, sql: str):
    return [column.name for column in execute_script(sql, db)[-1].columns]


def plan(db: Database, sql: str):
    return execute_script(sql, db)[-1].planned


def explain(db: Database, sql: str) -> str:
    return "\n".join(row[0] for row in execute_script(f"EXPLAIN {sql}", db)[-1].rows)


# -- what the rows are -------------------------------------------------------


def test_an_inner_join_keeps_only_matching_pairs(db: Database):
    assert rows(
        db, "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
    ) == [("ada", 100), ("ada", 250), ("alan", 80), ("grace", 400), ("grace", 50)]


def test_the_orphan_and_the_null_key_are_both_dropped(db: Database):
    # Order 15 points at a user that does not exist; order 16 has a NULL key.
    # An inner join drops both, and for different reasons: no match, and
    # `NULL = anything` is UNKNOWN rather than TRUE.
    totals = [
        total
        for _, total in rows(
            db, "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        )
    ]
    assert 7 not in totals
    assert 5 not in totals


def test_a_comma_join_is_the_same_thing_written_differently(db: Database):
    explicit = rows(
        db, "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
    )
    comma = rows(db, "SELECT u.name, o.total FROM users u, orders o WHERE u.id = o.user_id")
    assert sorted(explicit) == sorted(comma)


def test_a_comma_join_with_no_condition_is_a_cross_product(db: Database):
    assert len(rows(db, "SELECT u.id, o.id FROM users u, orders o")) == 3 * 7


def test_a_three_table_join(db: Database):
    db.create_table(
        "items",
        Schema.of(
            Column("id", DataType.INTEGER, nullable=False, primary_key=True),
            Column("order_id", DataType.INTEGER),
            Column("sku", DataType.TEXT),
        ),
    )
    db.insert_many("items", [(100, 10, "a"), (101, 10, "b"), (102, 13, "c")])
    assert rows(
        db,
        """
        SELECT u.name, i.sku
        FROM users u JOIN orders o ON u.id = o.user_id
                     JOIN items i ON o.id = i.order_id
    """,
    ) == [("ada", "a"), ("ada", "b"), ("grace", "c")]


def test_a_self_join_needs_aliases_and_works_with_them(db: Database):
    pairs = rows(
        db,
        """
        SELECT a.name, b.name FROM users a JOIN users b ON a.city = b.city
        WHERE a.id < b.id
    """,
    )
    assert pairs == [("ada", "alan")]


def test_the_same_table_twice_without_aliases_is_refused(db: Database):
    with pytest.raises(BindingError, match=r"used twice in FROM"):
        rows(db, "SELECT * FROM users JOIN users ON users.id = users.id")


def test_star_expands_across_every_table_and_qualifies_the_names(db: Database):
    assert names(db, "SELECT * FROM users u JOIN orders o ON u.id = o.user_id") == [
        "u.id",
        "u.name",
        "u.city",
        "o.id",
        "o.user_id",
        "o.total",
    ]


def test_a_qualified_star_takes_one_table(db: Database):
    assert names(db, "SELECT o.* FROM users u JOIN orders o ON u.id = o.user_id") == [
        "o.id",
        "o.user_id",
        "o.total",
    ]


def test_a_column_in_both_tables_is_ambiguous(db: Database):
    with pytest.raises(BindingError, match=r"'id' is ambiguous.*u.id.*o.id"):
        rows(db, "SELECT id FROM users u JOIN orders o ON u.id = o.user_id")


def test_an_alias_replaces_the_table_name(db: Database):
    with pytest.raises(BindingError, match=r"an alias replaces the table name"):
        rows(db, "SELECT users.name FROM users u JOIN orders o ON u.id = o.user_id")


def test_a_where_clause_runs_after_the_join(db: Database):
    assert rows(
        db,
        """
        SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id
        WHERE o.total > 200
    """,
    ) == [("ada", 250), ("grace", 400)]


# -- aggregation -------------------------------------------------------------


def test_count_star_counts_rows(db: Database):
    assert rows(db, "SELECT COUNT(*) FROM users") == [(3,)]
    assert names(db, "SELECT COUNT(*) FROM users") == ["count(*)"]


def test_count_of_a_column_counts_non_null_values(db: Database):
    # The distinction COUNT(*) and COUNT(x) exist to make. Order 16 has a NULL
    # user_id, so it counts as a row and not as a value.
    assert rows(db, "SELECT COUNT(*), COUNT(user_id) FROM orders") == [(7, 6)]


def test_an_aggregate_over_no_rows(db: Database):
    # COUNT is 0; everything else is NULL. Not zero: SUM over nothing has no
    # value, and calling it zero would make an empty table and a table of zeros
    # indistinguishable.
    assert rows(
        db,
        "SELECT COUNT(*), SUM(total), AVG(total), MIN(total) FROM orders WHERE total < 0",
    ) == [(0, None, None, None)]


def test_a_grouped_aggregate_over_no_rows_returns_nothing(db: Database):
    # The other half of the rule above: with no keys there is always exactly
    # one group, and with keys there are as many groups as there are values.
    assert (
        rows(db, "SELECT total, COUNT(*) FROM orders WHERE total < 0 GROUP BY total") == []
    )


def test_avg_is_a_float_even_over_integers(db: Database):
    # (1 + 2 + 3) / 3 is 2.0, not 2. Truncating because the column was INTEGER
    # would be the kind of quiet wrongness this project exists to avoid.
    (average,) = rows(db, "SELECT AVG(id) FROM users")
    assert average == (2.0,)
    assert isinstance(average[0], float)


def test_sum_and_avg_ignore_nulls_rather_than_treating_them_as_zero(db: Database):
    # AVG over [1, 1, 2, 3, 3, 99] and not over seven values including a NULL.
    assert rows(db, "SELECT SUM(user_id), COUNT(user_id) FROM orders") == [(109, 6)]


def test_group_by_produces_one_row_per_group(db: Database):
    assert sorted(rows(db, "SELECT city, COUNT(*) FROM users GROUP BY city")) == [
        ("london", 2),
        ("ny", 1),
    ]


def test_having_filters_groups_not_rows(db: Database):
    assert rows(
        db,
        """
        SELECT u.name, SUM(o.total) AS spend
        FROM users u JOIN orders o ON u.id = o.user_id
        GROUP BY u.name HAVING SUM(o.total) > 100
        ORDER BY spend DESC
    """,
    ) == [("grace", 450), ("ada", 350)]


def test_an_aggregate_is_computed_once_even_when_named_twice(db: Database):
    # `COUNT(*)` in both the select list and the HAVING is one accumulator.
    result = execute_script(
        "SELECT city, COUNT(*) FROM users GROUP BY city HAVING COUNT(*) > 1", db
    )[-1]
    assert list(result.rows) == [("london", 2)]
    aggregate = next(
        node
        for node in walk_physical(result.planned.root)
        if node.node_type == "PhysicalAggregate"
    )
    assert len(aggregate.aggregates) == 1


def test_a_column_not_in_group_by_is_refused(db: Database):
    with pytest.raises(BindingError, match=r"must appear in GROUP BY"):
        rows(db, "SELECT name, city, COUNT(*) FROM users GROUP BY city")


def test_an_aggregate_in_where_is_refused_and_says_to_use_having(db: Database):
    with pytest.raises(BindingError, match=r"cannot appear in WHERE.*use HAVING"):
        rows(db, "SELECT city FROM users WHERE COUNT(*) > 1 GROUP BY city")


def test_having_without_group_by_needs_an_aggregate(db: Database):
    with pytest.raises(BindingError, match=r"HAVING without GROUP BY needs an aggregate"):
        rows(db, "SELECT name FROM users HAVING name = 'ada'")


def test_a_bare_aggregate_needs_no_group_by(db: Database):
    assert rows(db, "SELECT COUNT(*) FROM orders HAVING COUNT(*) > 1") == [(7,)]


# -- ordering and limiting ---------------------------------------------------


def test_order_by_ascending_then_descending(db: Database):
    assert [t for (t,) in rows(db, "SELECT total FROM orders ORDER BY total")] == [
        5,
        7,
        50,
        80,
        100,
        250,
        400,
    ]
    assert [t for (t,) in rows(db, "SELECT total FROM orders ORDER BY total DESC")] == [
        400,
        250,
        100,
        80,
        50,
        7,
        5,
    ]


def test_nulls_sort_last_ascending_and_first_descending(db: Database):
    # PostgreSQL's default, and the opposite of SQLite's. There is no right
    # answer; there is a wrong one, which is comparing NULL to a number.
    ascending = [u for (u,) in rows(db, "SELECT user_id FROM orders ORDER BY user_id")]
    assert ascending[-1] is None
    descending = [
        u for (u,) in rows(db, "SELECT user_id FROM orders ORDER BY user_id DESC")
    ]
    assert descending[0] is None


def test_order_by_an_alias(db: Database):
    assert rows(db, "SELECT total AS t FROM orders ORDER BY t DESC LIMIT 2") == [
        (400,),
        (250,),
    ]


def test_order_by_an_ordinal(db: Database):
    assert rows(db, "SELECT id, total FROM orders ORDER BY 2 DESC LIMIT 1") == [(13, 400)]


def test_order_by_something_not_selected_is_refused(db: Database):
    with pytest.raises(BindingError, match=r"ORDER BY must name something in the SELECT"):
        rows(db, "SELECT id FROM orders ORDER BY total")


def test_several_sort_keys_are_applied_most_significant_first(db: Database):
    assert rows(
        db,
        """
        SELECT city, name FROM users ORDER BY city, name DESC
    """,
    ) == [("london", "alan"), ("london", "ada"), ("ny", "grace")]


def test_limit_and_offset(db: Database):
    assert rows(db, "SELECT total FROM orders ORDER BY total LIMIT 3") == [
        (5,),
        (7,),
        (50,),
    ]
    assert rows(db, "SELECT total FROM orders ORDER BY total LIMIT 2 OFFSET 3") == [
        (80,),
        (100,),
    ]


def test_a_limit_larger_than_the_table_is_not_an_error(db: Database):
    assert len(rows(db, "SELECT id FROM users LIMIT 500")) == 3


# -- what the planner did ----------------------------------------------------


def test_an_equijoin_uses_a_hash_join(db: Database):
    planned = plan(db, "SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id")
    assert any(isinstance(n, PhysicalHashJoin) for n in walk_physical(planned.root))


def test_a_range_join_falls_back_to_nested_loops(db: Database):
    # There is no key to hash. This is why a range join is slow in every
    # engine, and the plan says so rather than silently being quadratic.
    planned = plan(db, "SELECT u.id FROM users u JOIN orders o ON u.id < o.total")
    assert any(isinstance(n, PhysicalNestedLoopJoin) for n in walk_physical(planned.root))


def test_the_smaller_side_is_the_build_side(db: Database):
    # Memory is proportional to the build side, and the cost model says so by
    # charging a build more than a probe. `users` has 3 rows and `orders` 7.
    planned = plan(db, "SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id")
    join = next(n for n in walk_physical(planned.root) if isinstance(n, PhysicalHashJoin))
    assert join.left.table_name == "users"


def test_a_single_table_predicate_is_pushed_below_the_join(db: Database):
    # `city = 'london'` shrinks the input to the join rather than filtering its
    # output. Pushing down can never be worse, which is why it is a rewrite and
    # not a costed alternative.
    text = explain(
        db,
        """
        SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id
        WHERE u.city = 'london'
    """,
    )
    filter_line = next(i for i, line in enumerate(text.split("\n")) if "Filter" in line)
    join_line = next(i for i, line in enumerate(text.split("\n")) if "Join" in line)
    assert filter_line > join_line, "the filter sits below the join in the tree"


def test_a_pushed_predicate_can_still_use_an_index(tmp_path: Path):
    """Pushdown is what makes the index reachable at all.

    A predicate left above the join is applied to join output, where no index
    exists. Pushed to the scan it becomes an access-path decision again, and
    on a table this size the index wins it.
    """
    with Database.open(tmp_path / "idx.chendb", page_size=4096) as db:
        db.create_table("users", USERS)
        db.create_table("orders", ORDERS)
        db.insert_many(
            "users", [(n, f"n{n}", "london" if n == 7 else "ny") for n in range(600)]
        )
        db.insert_many("orders", [(n, n, n) for n in range(50)])
        db.create_index("users_city", "users", "city")
        db.analyze()

        planned = plan(
            db,
            """
            SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id
            WHERE u.city = 'london'
        """,
        )
        assert any(isinstance(n, PhysicalIndexScan) for n in walk_physical(planned.root))


def test_the_explain_output_groups_its_decisions(db: Database):
    text = explain(db, "SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id")
    assert "how to read users" in text
    assert "how to read orders" in text
    assert "what order to join in" in text


def test_the_plan_reads_bottom_up_in_sql_evaluation_order(db: Database):
    text = explain(
        db,
        """
        SELECT u.city, COUNT(*) AS n FROM users u JOIN orders o ON u.id = o.user_id
        WHERE o.total > 10 GROUP BY u.city HAVING COUNT(*) > 1
        ORDER BY n DESC LIMIT 5
    """,
    )
    order = [
        line.strip().removeprefix("└─ ").split()[0]
        for line in text.split("\n")
        if line.startswith(("Physical", " ")) and "Physical" in line and "cost=" in line
    ]
    assert order[0].startswith("PhysicalLimit")
    assert order.index("PhysicalSort") < order.index("PhysicalProject")
    assert order.index("PhysicalProject") < order.index("PhysicalAggregate")
    join = next(i for i, name in enumerate(order) if "Join" in name)
    assert order.index("PhysicalAggregate") < join


def test_join_order_is_chosen_rather_than_taken_from_the_query(tmp_path: Path):
    """The point of the whole exercise: written order is not run order.

    ``big`` is scanned first because the query says so, and the planner joins
    ``small`` first anyway. Its estimate is a twentieth of the size, so it is
    the cheaper thing to build a hash table on.
    """
    with Database.open(tmp_path / "order.chendb", page_size=4096) as db:
        db.create_table("big", ORDERS)
        db.create_table("small", USERS)
        db.insert_many("big", [(n, n % 20, n) for n in range(400)])
        db.insert_many("small", [(n, f"n{n}", "x") for n in range(20)])
        db.analyze()

        planned = plan(db, "SELECT b.id FROM big b JOIN small s ON b.user_id = s.id")
        join = next(
            n for n in walk_physical(planned.root) if isinstance(n, PhysicalHashJoin)
        )
        assert join.left.table_name == "small", "the small side is built, not the big one"


def test_a_pushed_predicate_is_costed_against_its_own_tables_statistics(db: Database):
    """A bound index addresses the *joined* row; statistics do not.

    ``s.total`` is index 5 of the joined row and column 2 of ``orders``. Asking
    a three-column table for its column 5 gets nothing, the selectivity falls
    back to a default, the filter's estimate collapses to almost no rows, and
    the planner concludes a nested loop over them is nearly free. It did.
    """
    db.analyze()
    result = execute_script(
        "EXPLAIN SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 20",
        db,
    )[-1]
    nodes = walk_physical(result.planned.root)

    predicate_filter = next(n for n in nodes if n.node_type == "PhysicalFilter")
    assert predicate_filter.estimated.rows > 2, (
        "5 of 7 orders are over 20; an estimate near zero means the statistics "
        "lookup missed"
    )
    assert any(isinstance(node, PhysicalHashJoin) for node in nodes), (
        "and a collapsed estimate is what makes a nested loop look cheap"
    )


# --------------------------------------------------------------------------
# Outer joins (Milestone 18)
# --------------------------------------------------------------------------

# The fixture is built for this. `users` has grace, who has orders; `orders` has
# an orphan (user 99) and a NULL key: so a LEFT join has something to preserve,
# a RIGHT join has something else, and neither is testing an empty set.


def test_a_left_join_keeps_a_user_with_no_orders(db: Database):
    db.insert("users", (4, "edsger", "amsterdam"))
    result = rows(
        db,
        "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "ORDER BY u.name, o.total;",
    )
    assert ("edsger", None) in result
    assert sum(1 for row in result if row[0] == "edsger") == 1, "exactly one NULL row"
    assert ("ada", 100) in result, "and the matched rows are unchanged"


def test_a_left_join_null_extends_every_column_of_the_missing_side(db: Database):
    db.insert("users", (4, "edsger", "amsterdam"))
    (row,) = rows(
        db,
        "SELECT u.name, o.id, o.user_id, o.total FROM users u "
        "LEFT JOIN orders o ON u.id = o.user_id WHERE u.id = 4;",
    )
    assert row == ("edsger", None, None, None), "not just the key, the whole side"


def test_a_right_join_keeps_the_orphan_and_the_null_key(db: Database):
    result = rows(
        db,
        "SELECT u.name, o.id FROM users u RIGHT JOIN orders o ON u.id = o.user_id "
        "ORDER BY o.id;",
    )
    assert (None, 15) in result, "order 15 belongs to user 99, who does not exist"
    assert (None, 16) in result, "order 16 has a NULL key, so it can never match"
    assert len(result) == len(ORDER_ROWS)


def test_a_full_join_keeps_both_sides(db: Database):
    db.insert("users", (4, "edsger", "amsterdam"))
    result = rows(
        db,
        "SELECT u.id, o.id FROM users u FULL JOIN orders o ON u.id = o.user_id;",
    )
    assert (4, None) in result, "the user with no orders"
    assert (None, 15) in result, "the order with no user"
    assert (None, 16) in result, "the order with a NULL key"
    # Five matched pairs, one unmatched user, two unmatched orders.
    assert len(result) == 8


def test_an_inner_join_still_drops_both(db: Database):
    db.insert("users", (4, "edsger", "amsterdam"))
    result = rows(db, "SELECT u.id, o.id FROM users u JOIN orders o ON u.id = o.user_id;")
    assert all(row[0] is not None and row[1] is not None for row in result)
    assert len(result) == 5


@pytest.mark.parametrize("spelling", ["LEFT OUTER", "RIGHT OUTER", "FULL OUTER"])
def test_the_outer_keyword_is_noise(db: Database, spelling: str):
    bare = spelling.removesuffix(" OUTER")
    with_outer = rows(
        db, f"SELECT u.id, o.id FROM users u {spelling} JOIN orders o ON u.id = o.user_id;"
    )
    without = rows(
        db, f"SELECT u.id, o.id FROM users u {bare} JOIN orders o ON u.id = o.user_id;"
    )
    assert with_outer == without


# -- the ON is not the WHERE, and this is the whole point --------------------


def test_a_condition_in_the_on_restricts_matching_not_survival(db: Database):
    """The single most important property of an outer join.

    ``ON … AND o.total > 200`` decides which orders *count as a match*. Every user
    survives regardless. The ones whose orders were all too small come back
    NULL-extended, exactly as if they had no orders at all.
    """
    result = rows(
        db,
        "SELECT u.name, o.total FROM users u LEFT JOIN orders o "
        "ON u.id = o.user_id AND o.total > 200 ORDER BY u.name, o.total;",
    )
    assert [row[0] for row in result] == ["ada", "alan", "grace"], "every user survives"
    assert ("alan", None) in result, "alan's only order is 80, so he is NULL-extended"
    assert ("ada", 250) in result
    assert ("ada", None) not in result, "ada matched, so she is not extended"


def test_the_same_condition_in_the_where_removes_the_extended_rows(db: Database):
    """And the contrast that makes it a real distinction.

    The WHERE runs *after* NULL-extension, and ``NULL > 200`` is NULL rather than
    TRUE, so it rejects every preserved row and the outer join collapses to an
    inner one. Both queries are correct and they are different queries.
    """
    result = rows(
        db,
        "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200 ORDER BY u.name, o.total;",
    )
    assert result == [("ada", 250), ("grace", 400)]


def test_the_anti_join_idiom_finds_the_rows_with_no_partner(db: Database):
    # `WHERE <null-supplied column> IS NULL` after a LEFT JOIN is *the* way to ask
    # "which rows have no match", and it only works because the join preserved
    # them for the filter to find.
    db.insert("users", (4, "edsger", "amsterdam"))
    result = rows(
        db,
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.id IS NULL ORDER BY u.name;",
    )
    assert result == [("edsger",)]


def test_an_outer_join_with_no_equality_uses_a_nested_loop(db: Database):
    # No key to hash, so the hash join is not a candidate at all: and the
    # NULL-extension has to work in the nested loop too.
    # 100 is above every user_id in `orders` (the largest is the orphan's 99), so
    # this row matches nothing and has to be preserved.
    db.insert("users", (100, "edsger", "amsterdam"))
    sql = (
        "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON u.id < o.user_id "
        "ORDER BY u.id, o.id;"
    )
    assert any(
        isinstance(node, PhysicalNestedLoopJoin)
        for node in walk_physical(plan(db, sql).root)
    )
    result = rows(db, sql)
    assert (100, None) in result


# -- what the planner may and may not do -------------------------------------


def test_an_outer_joins_on_condition_is_not_pushed_below_it(db: Database):
    """The bug this milestone had to avoid, stated as a test.

    ``plan_select`` pools the WHERE with every *inner* ``ON`` and pushes each
    single-table conjunct down to its scan, which is sound and valuable. Doing it
    to an outer join's ``ON`` would filter the rows the join exists to preserve.
    """
    inner = explain(
        db, "SELECT * FROM users u JOIN orders o ON u.id = o.user_id AND o.total > 200;"
    )
    assert "Filter" in inner, "an inner join's condition is pushed below it"

    outer = explain(
        db,
        "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id AND o.total > 200;",
    )
    join_lines = [line for line in outer.splitlines() if "Join" in line]
    assert join_lines and "total > 200" in join_lines[0], (
        "an outer join's condition stays at the join:\n" + outer
    )


def test_a_predicate_on_the_preserved_side_is_still_pushed_down(db: Database):
    # The other half: pushing into the side that cannot be NULL-extended is
    # always legal, and giving it up would make every outer join slower than it
    # needs to be.
    outer = explain(
        db,
        "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE u.city = 'london';",
    )
    lines = outer.splitlines()
    filter_at = next(i for i, line in enumerate(lines) if "Filter" in line)
    join_at = next(i for i, line in enumerate(lines) if "Join" in line)
    assert filter_at > join_at, "the filter belongs below the join:\n" + outer


def test_a_predicate_on_the_null_supplied_side_is_not_pushed_down(db: Database):
    """And the reason for the asymmetry: pushed below and consumed, this would
    leave the NULL-extended rows in the result, which the WHERE must reject.

    The predicate has to be one that can be TRUE about a NULL-extended row, or
    there is no longer an outer join to protect. This one can: ``NULL > 200`` is
    NULL, ``NULL IS NULL`` is TRUE, so a preserved row passes. Milestone 19
    turned the plain ``WHERE o.total > 200`` version of this test into an inner
    join and pushed the predicate down, which is correct and is the point of that
    milestone, see :func:`test_a_null_rejecting_where_turns_a_left_join_inner`.
    """
    outer = explain(
        db,
        "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200 OR o.total IS NULL;",
    )
    assert "LEFT" in outer, "the join must still be outer for this to test anything"
    lines = outer.splitlines()
    filter_at = next(i for i, line in enumerate(lines) if "Filter" in line)
    join_at = next(i for i, line in enumerate(lines) if "Join" in line)
    assert filter_at < join_at, "the filter belongs above the join:\n" + outer


def test_explain_names_the_join_flavour(db: Database):
    # A plan display that showed an outer join as an inner one would be describing
    # something that is not happening: and join order is exactly what an outer
    # join constrains.
    outer = explain(db, "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id;")
    assert "LEFT" in outer
    assert "outer join" in outer, "the order decision must say what constrained it"

    inner = explain(db, "SELECT * FROM users u JOIN orders o ON u.id = o.user_id;")
    assert "LEFT" not in inner
    assert "outer join" not in inner


def test_an_outer_join_is_estimated_above_its_preserved_input(db: Database):
    """An outer join has a floor an inner join does not.

    Every preserved row appears whether it matched or not, so an estimate below
    the preserved side's row count is not merely imprecise. It is impossible, and
    it would make every operator above the join look cheaper than it is.
    """
    planned = plan(
        db,
        "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id AND o.total > 100000;",
    )
    join = next(
        node
        for node in walk_physical(planned.root)
        if isinstance(node, PhysicalHashJoin | PhysicalNestedLoopJoin)
    )
    assert join.estimated.rows >= len(USER_ROWS)


def test_an_outer_join_is_not_costed_as_a_cross_product(db: Database):
    """The estimator reads the join's own conditions, wherever they came from.

    It used to read positions into the shared conjunct pool (which an outer join
    contributes nothing to) so an outer join's equality was invisible to it and
    every one was costed as a full cross product.
    """
    outer = plan(db, "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id;")
    join = next(
        node
        for node in walk_physical(outer.root)
        if isinstance(node, PhysicalHashJoin | PhysicalNestedLoopJoin)
    )
    assert join.estimated.rows < len(USER_ROWS) * len(ORDER_ROWS)


# --------------------------------------------------------------------------
# Outer-join simplification (Milestone 19)
# --------------------------------------------------------------------------

# Milestone 18 ran an outer join exactly where it was written and never asked
# whether it had to be one. These test the rule that asks. The interesting half
# is not that it fires: it is the list of shapes where it must not, because each
# of those is a query whose rows would quietly change.

#: A third table, so the chain is long enough for one join to prove another and
#: for reordering to have somewhere to go.
TAGS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("order_id", DataType.INTEGER),
    Column("label", DataType.TEXT),
)
TAG_ROWS = [(20, 10, "gift"), (21, 13, "rush"), (22, 99, "orphan")]


@pytest.fixture
def three(tmp_path: Path):
    with Database.open(tmp_path / "chain.chendb", page_size=4096) as handle:
        handle.create_table("users", USERS)
        handle.create_table("orders", ORDERS)
        handle.create_table("tags", TAGS)
        handle.insert_many("users", USER_ROWS)
        handle.insert_many("orders", ORDER_ROWS)
        handle.insert_many("tags", TAG_ROWS)
        yield handle


def joins_of(db: Database, sql: str) -> list[tuple[bool, bool]]:
    """What each join in the plan actually preserves, innermost first."""
    return [
        (node.preserve_left, node.preserve_right)
        for node in reversed(walk_physical(plan(db, sql).root))
        if isinstance(node, PhysicalJoin)
    ]


def fired(db: Database, sql: str) -> bool:
    return "simplify_outer_joins" in plan(db, sql).rewrites


INNER, LEFT_OUTER, RIGHT_OUTER, FULL_OUTER = (
    (False, False),
    (True, False),
    (False, True),
    (True, True),
)


def test_a_null_rejecting_where_turns_a_left_join_inner(db: Database):
    """The rule, in one query.

    ``o.total`` is NULL in every row the LEFT join preserved, ``NULL > 200`` is
    NULL, and a WHERE keeps only TRUE. Not one preserved row can reach the
    output, so preserving them was work nobody could observe.
    """
    sql = (
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200;"
    )
    assert fired(db, sql)
    assert joins_of(db, sql) == [INNER]


def test_the_rewrite_returns_exactly_what_the_outer_join_returned(
    db: Database, monkeypatch: pytest.MonkeyPatch
):
    """The contract of a rewrite rule, checked rather than asserted.

    ``apply_rules`` reads the module-level ``RULES`` at call time, so removing
    one entry is enough to plan the same SQL both ways. Every query below has to
    come back identical, and the ones the rule declines to touch are in the list
    on purpose: an accidentally-firing rule is caught by the same comparison.
    """
    queries = [
        "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200 ORDER BY u.name, o.total;",
        "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total IS NULL ORDER BY u.name;",
        "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200 OR u.city = 'ny' ORDER BY u.name, o.total;",
        "SELECT u.name, o.id FROM users u RIGHT JOIN orders o ON u.id = o.user_id "
        "WHERE u.city = 'london' ORDER BY o.id;",
        "SELECT u.name, o.id FROM users u FULL JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 100 ORDER BY o.id;",
        "SELECT u.name, o.id FROM users u FULL JOIN orders o ON u.id = o.user_id "
        "WHERE u.city = 'ny' ORDER BY o.id;",
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE NOT (o.total = 1) ORDER BY u.name;",
        "SELECT COUNT(*) FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200;",
    ]
    with_rule = [rows(db, sql) for sql in queries]

    monkeypatch.setattr(
        rules, "RULES", tuple(r for r in rules.RULES if r.name != "simplify_outer_joins")
    )
    for sql, expected in zip(queries, with_rule, strict=True):
        assert not fired(db, sql), "the rule is meant to be switched off here"
        assert rows(db, sql) == expected, f"the rewrite changed the answer to:\n{sql}"


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        # IS NULL is the anti-join idiom, and the one predicate that goes TRUE
        # about a row the join invented. Rewriting it would break the single most
        # common reason anybody writes an outer join by hand.
        ("o.id IS NULL", LEFT_OUTER),
        # One survivable branch of an OR is enough to keep a preserved row alive.
        ("o.total > 200 OR u.city = 'ny'", LEFT_OUTER),
        ("o.total > 200 OR o.total IS NULL", LEFT_OUTER),
        # Nothing to do with the NULL-supplied side at all.
        ("u.city = 'london'", LEFT_OUTER),
        # And the ones that do reject.
        ("o.total > 200", INNER),
        ("o.total IS NOT NULL", INNER),
        ("NOT (o.total = 1)", INNER),
        ("o.total > 200 AND u.city = 'ny'", INNER),
        ("o.total + 1 > 200", INNER),
        ("o.user_id = u.id", INNER),
    ],
)
def test_which_predicates_collapse_a_left_join(
    db: Database, where: str, expected: tuple[bool, bool]
):
    sql = (
        f"SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE {where};"
    )
    assert joins_of(db, sql) == [expected]


def test_a_right_join_is_reduced_by_a_predicate_on_its_left(db: Database):
    # The mirror image, and it is not symmetric in the code: a RIGHT join
    # NULL-extends everything accumulated to its left, not one named table.
    sql = (
        "SELECT o.id FROM users u RIGHT JOIN orders o ON u.id = o.user_id "
        "WHERE u.city = 'london';"
    )
    assert joins_of(db, sql) == [INNER]

    kept = (
        "SELECT o.id FROM users u RIGHT JOIN orders o ON u.id = o.user_id "
        "WHERE u.city IS NULL;"
    )
    assert joins_of(db, kept) == [RIGHT_OUTER]


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        # Reject the right side and the left-preserved rows die: what is left is
        # the matches plus the unmatched right, which is a RIGHT join.
        ("o.total > 100", RIGHT_OUTER),
        ("u.city = 'ny'", LEFT_OUTER),
        ("u.city = 'ny' AND o.total > 100", INNER),
        ("u.city IS NULL OR o.total IS NULL", FULL_OUTER),
    ],
)
def test_a_full_join_gives_up_one_side_at_a_time(
    db: Database, where: str, expected: tuple[bool, bool]
):
    """A join is two booleans, not four names, which is why this is one rule.

    ``FULL`` is the only kind that can be reduced and still be outer, and it is
    where a rewrite written as a table of name-to-name special cases would have
    needed two more entries nobody would have thought to add.
    """
    sql = (
        f"SELECT u.name FROM users u FULL JOIN orders o ON u.id = o.user_id WHERE {where};"
    )
    assert joins_of(db, sql) == [expected]


# -- what counts as evidence -------------------------------------------------


def test_a_later_inner_join_proves_an_earlier_outer_one(three: Database):
    """No WHERE at all, and the LEFT join still collapses.

    ``o.id = t.order_id`` is NULL for every row the LEFT join preserved, and an
    inner join discards a row its ``ON`` does not accept. So the third join is
    what proves the first, which is a common shape and one a rule that only read
    the WHERE would miss.
    """
    sql = (
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "JOIN tags t ON o.id = t.order_id;"
    )
    assert joins_of(three, sql) == [INNER, INNER]
    assert sorted(rows(three, sql)) == [("ada",), ("grace",)]


def test_a_later_outer_join_proves_nothing(three: Database):
    # A LEFT join above preserves the rows the LEFT join below invented, so its
    # ON never gets to reject them and is not admissible evidence.
    sql = (
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "LEFT JOIN tags t ON o.id = t.order_id;"
    )
    assert joins_of(three, sql) == [LEFT_OUTER, LEFT_OUTER]


def test_an_outer_joins_own_on_is_not_evidence_about_itself(db: Database):
    """The mistake this rule most easily makes.

    ``ON b.x = 5`` cannot be TRUE about a NULL-extended row either, and it is
    still not a reason to rewrite: an outer join's ``ON`` decides which rows
    *match*, and the rows that do not match are exactly the ones it preserves.
    Treating the ON like a WHERE is the whole difference between the two.
    """
    sql = (
        "SELECT u.name, o.total FROM users u "
        "LEFT JOIN orders o ON u.id = o.user_id AND o.total > 200;"
    )
    assert joins_of(db, sql) == [LEFT_OUTER]
    assert sorted(rows(db, sql)) == [("ada", 250), ("alan", None), ("grace", 400)]


def test_having_is_not_evidence(db: Database):
    # HAVING runs after grouping, so a NULL that reaches a group is a longer
    # argument than this rule makes. Declining is a missed rewrite, not a wrong
    # answer, and the rows below are what makes that the right call to check.
    sql = (
        "SELECT u.name, COUNT(o.id) FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "GROUP BY u.name HAVING COUNT(o.id) > 0;"
    )
    assert joins_of(db, sql) == [LEFT_OUTER]


def test_the_rule_reaches_a_where_under_a_group_by(db: Database):
    # The WHERE is below the aggregate in the plan, so the rule has to walk past
    # the aggregate to find it. The three rules written before joins existed do
    # not, which is why this one descends generically.
    sql = (
        "SELECT u.city, COUNT(*) FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200 GROUP BY u.city;"
    )
    assert fired(db, sql)
    assert joins_of(db, sql) == [INNER]


# -- what the rewrite buys ---------------------------------------------------


def test_the_rewrite_re_enables_pushdown(db: Database):
    """The first of the two second-hand wins, and the larger one here.

    Milestone 18 protects a NULL-supplied table from pushdown, correctly. Once
    the join is inner there is nothing to protect, and the very predicate that
    proved the rewrite is what gets pushed: it filters ``orders`` before the join
    instead of filtering the join's output.
    """
    text = explain(
        db,
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200;",
    )
    lines = text.splitlines()
    filter_at = next(i for i, line in enumerate(lines) if "Filter" in line)
    join_at = next(i for i, line in enumerate(lines) if "Join" in line)
    assert filter_at > join_at, "the filter belongs below the join now:\n" + text


def test_the_rewrite_re_enables_reordering(three: Database):
    """The second, and the reason this rule is in the milestone at all.

    An outer join constrains the join-order search, and ``EXPLAIN`` says which
    ones did. Proving them inner removes the constraint entirely: the order
    decision stops mentioning an outer join because there is not one left.
    """
    constrained = "outer join"
    kept = explain(
        three,
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "LEFT JOIN tags t ON o.id = t.order_id;",
    )
    assert constrained in kept

    freed = explain(
        three,
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "LEFT JOIN tags t ON o.id = t.order_id WHERE t.label = 'gift';",
    )
    assert constrained not in freed, "both joins are inner now:\n" + freed


def test_a_pushed_predicate_can_use_an_index_after_the_rewrite(tmp_path: Path):
    """Pushdown is worth more than one scan's worth of filtering.

    A predicate that reaches a scan can be answered by an index instead of by
    reading the table, which is the difference between the rewrite saving a few
    comparisons and it saving the scan.
    """
    with Database.open(tmp_path / "indexed.chendb", page_size=4096) as db:
        db.create_table("users", USERS)
        db.create_table("orders", ORDERS)
        db.insert_many("users", USER_ROWS)
        db.insert_many("orders", [(n, n % 3, n) for n in range(200)])
        db.create_index("orders_total", "orders", "total")
        db.analyze()

        sql = (
            "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
            "WHERE o.total = 42;"
        )
        assert joins_of(db, sql) == [INNER]
        assert any(
            isinstance(node, PhysicalIndexScan)
            for node in walk_physical(plan(db, sql).root)
        ), "the pushed predicate should reach the index:\n" + explain(db, sql)


def test_explain_names_the_rewrite(db: Database):
    # A plan that is mysteriously different is only useful if you can see why,
    # and this rule changes a join's *meaning* in the display, so it has to say
    # so rather than let the reader wonder where the LEFT went.
    text = explain(
        db,
        "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 200;",
    )
    assert "simplify_outer_joins" in text
    assert "LEFT" not in text


# -- the estimate the rewrite made matter more -------------------------------


@pytest.mark.parametrize("kind", ["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"])
def test_an_equijoin_is_estimated_from_distinct_values_not_row_counts(
    tmp_path: Path, kind: str
):
    """A foreign-key join used to be estimated at the size of the wrong side.

    Fifty users, four thousand orders, eighty orders each, so the join produces
    four thousand rows whichever way it is written. ``join_selectivity`` divided
    by ``max(row_count)``, which is 4,000, and got ``50 * 4000 / 4000``. Fifty.

    ``distinct_join_selectivity`` has spelled the right formula since Milestone 6
    and nothing ever called it. This surfaced measuring Milestone 19, because
    that milestone hands rewritten joins back to the order search, and a search
    cannot choose between plans it cannot size. The error compounds upward: 80x
    on two tables is 6,400x on three.
    """
    with Database.open(tmp_path / "estimate.chendb", page_size=4096) as db:
        db.create_table("users", USERS)
        db.create_table("orders", ORDERS)
        db.insert_many("users", [(n, f"u{n}", "x") for n in range(50)])
        db.insert_many("orders", [(n, n % 50, n) for n in range(4000)])
        db.analyze()

        sql = f"SELECT u.name FROM users u {kind} orders o ON u.id = o.user_id"
        estimated = plan(db, sql).estimated_rows
        actual = len(rows(db, sql))
        assert actual == 4000
        assert 0.5 <= estimated / actual <= 2.0, (
            f"{kind} estimated {estimated} rows against {actual} actual"
        )


# --------------------------------------------------------------------------
# Three tables, and the two bugs two of them could not find (Milestone 21)
# --------------------------------------------------------------------------

# Both of these were shipped, both returned wrong answers, and both were
# invisible to every test and to 320,000 generated query pairs, because the
# generator built two tables and each needs three. They are regressions now;
# `tests/differential/generator.py` builds a grandchild so the class is covered
# rather than these two instances.

CHAIN = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("ref", DataType.INTEGER),
    Column("n", DataType.INTEGER),
)


@pytest.fixture
def chain(tmp_path: Path):
    """a, b and c, where only some of a has a b and only some of b has a c."""
    with Database.open(tmp_path / "chain3.chendb", page_size=4096) as handle:
        for name in ("a", "b", "c"):
            handle.create_table(name, CHAIN)
        handle.insert_many("a", [(1, 0, 10), (2, 0, 20), (3, 0, 30)])
        handle.insert_many("b", [(100, 1, 1), (101, 1, 2), (102, 2, -5)])
        handle.insert_many("c", [(200, 10, 7), (201, 20, 8)])
        yield handle


def test_a_table_after_an_outer_join_is_not_dropped(chain: Database):
    """The join-order search silently planned two tables out of three.

    ``_search_join_order`` keyed its dynamic-programming table by which *tables*
    a subplan covered, and enumerated subsets of ``range(len(relations))``. The
    two agree only when every input is a single table. After an outer join one
    input is a relation holding several, the two key spaces diverge, every
    lookup missed, and the search returned the seeded relation untouched.

    The plan then had no scan of ``c`` at all, and the residual compared ``a.n``
    against a column no operator had ever written, so the query returned **no
    rows**. Nothing detected it: an outer join with an inner join after it needs
    three tables to express, and the generator built two.
    """
    sql = (
        "SELECT a.id, b.id, c.id FROM a "
        "LEFT JOIN b ON a.id = b.ref "
        "JOIN c ON a.n = c.ref ORDER BY a.id, b.id;"
    )
    planned = plan(chain, sql)
    scanned = {
        node.table_name
        for node in walk_physical(planned.root)
        if isinstance(node, PhysicalSeqScan | PhysicalIndexScan)
    }
    assert scanned == {"a", "b", "c"}, f"the plan reads only {sorted(scanned)}"
    assert rows(chain, sql) == [(1, 100, 200), (1, 101, 200), (2, 102, 201)]


def test_an_inner_on_below_an_outer_join_is_not_a_where(chain: Database):
    """An ``ON`` that could not be pushed down floated up and became a filter.

    Milestone 18 stops a predicate being pushed into a table that an outer join
    can NULL-extend, which is right for a WHERE. It applied the same rule to
    every pooled conjunct, including an inner join's ``ON`` written *below* the
    outer join, and such a conjunct runs before the NULL-extension rather than
    after it.

    Over-protected, it could be pushed nowhere, so it landed in the residual,
    and a residual runs at the very top of the plan. There it rejected exactly
    the rows the ``RIGHT`` join existed to preserve. Predicates now carry which
    join they came from, and the protection only counts the outer joins below
    them.
    """
    sql = (
        "SELECT a.id, b.id, c.id FROM a "
        "JOIN b ON a.id = b.ref AND b.n > 100 "
        "RIGHT JOIN c ON b.id = c.ref ORDER BY c.id;"
    )
    # `b.n > 100` matches nothing, so the inner join is empty and every row of
    # `c` comes back NULL-extended. Dropping them is the bug.
    assert rows(chain, sql) == [(None, None, 200), (None, None, 201)]

    text = explain(chain, sql)
    lines = text.splitlines()
    filter_at = next(i for i, line in enumerate(lines) if "n > 100" in line)
    join_at = next(i for i, line in enumerate(lines) if "RIGHT" in line)
    assert filter_at > join_at, "the ON belongs below the outer join:\n" + text


def test_a_where_on_the_same_column_still_may_not_be_pushed(chain: Database):
    """The other half, and the reason the fix is per-predicate rather than off.

    The same column in the WHERE runs *after* the outer join, so it must still
    be evaluated above it and must still not reach ``b``'s scan. One predicate
    moved and one did not, which is the whole content of the fix.

    The WHERE has to be one that can be TRUE about a NULL-extended row, or
    Milestone 19 proves the outer join away and there is nothing left to
    protect. This one can, which is also why every preserved row survives it.
    """
    sql = (
        "SELECT a.id, b.id, c.id FROM a "
        "JOIN b ON a.id = b.ref "
        "RIGHT JOIN c ON b.id = c.ref WHERE b.n > 100 OR b.n IS NULL ORDER BY c.id;"
    )
    assert rows(chain, sql) == [(None, None, 200), (None, None, 201)]

    text = explain(chain, sql)
    lines = text.splitlines()
    filter_at = next(i for i, line in enumerate(lines) if "n > 100" in line)
    join_at = next(i for i, line in enumerate(lines) if "RIGHT" in line)
    assert filter_at < join_at, "the WHERE belongs above the outer join:\n" + text


def test_three_tables_agree_across_every_pair_of_join_kinds(chain: Database):
    """Sixteen chains, each checked against the shape it was written as.

    Not against SQLite (`tests/differential/` does that, over a corpus that now
    builds three tables) but against the row counts a reader can derive: the
    inner core is two rows, LEFT adds the `a` with no `b`, RIGHT adds the `c`
    with no `b`, and FULL adds both wherever the chain lets it.
    """
    for first in ("JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"):
        for second in ("JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"):
            sql = (
                f"SELECT a.id, b.id, c.id FROM a "
                f"{first} b ON a.id = b.ref "
                f"{second} c ON b.n = c.n ORDER BY a.id, b.id, c.id;"
            )
            result = rows(chain, sql)
            # Every row must be a real combination: a NULL only where the join
            # that produced it was allowed to invent one.
            assert all(len(row) == 3 for row in result)
            scanned = {
                node.table_name
                for node in walk_physical(plan(chain, sql).root)
                if isinstance(node, PhysicalSeqScan | PhysicalIndexScan)
            }
            assert scanned == {"a", "b", "c"}, f"{first} / {second} reads {scanned}"


# --------------------------------------------------------------------------
# Reordering across an outer join (Milestone 22)
# --------------------------------------------------------------------------

# Milestone 18 required every table written to an outer join's left to be
# joined before it. That is the safe over-approximation. The real requirement
# is the tables its own ON reads, and the difference is what lets an inner join
# written after an outer one run before it.

WIDE = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("k", DataType.INTEGER),
)
TAGGED = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("a_id", DataType.INTEGER),
)


@pytest.fixture
def wide(tmp_path: Path):
    """Twenty rows, four thousand rows, twenty rows.

    Deliberately lopsided: joining ``big`` last is worth measuring, and joining
    it first is what Milestone 18 was forced into. Four thousand rather than the
    twenty thousand the milestone document measures on, because
    ``execute_script`` stops at ``DEFAULT_MAX_ROWS`` and two plans that return
    different ten-thousand-row *prefixes* of the same answer would look like a
    reordering that changed it.
    """
    with Database.open(tmp_path / "wide.chendb", page_size=4096) as handle:
        handle.create_table("a", WIDE)
        handle.create_table("big", WIDE)
        handle.create_table("tag", TAGGED)
        handle.insert_many("a", [(n, n) for n in range(20)])
        handle.insert_many("big", [(n, n % 20) for n in range(4_000)])
        handle.insert_many("tag", [(n, n) for n in range(20)])
        handle.analyze()
        yield handle


def order_of(db: Database, sql: str) -> str:
    """The chosen join order, as ``EXPLAIN`` reports it."""
    return next(
        alternative.description.split(":")[0]
        for alternative in plan(db, sql).alternatives
        if alternative.decision == "what order to join in" and alternative.chosen
    )


def syntactic_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the planner back on Milestone 18's rule: everything written to the left."""
    original = physical._outer_constraints

    def patched(steps, first):
        return {
            position: replace(info, min_left=info.syntactic_left)
            for position, info in original(steps, first).items()
        }

    monkeypatch.setattr(physical, "_outer_constraints", patched)


REORDERABLE = (
    "SELECT a.id FROM a JOIN big ON a.k = big.k LEFT JOIN tag ON a.id = tag.a_id",
    "SELECT a.id FROM a JOIN big ON a.k = big.k LEFT JOIN tag ON a.id = tag.a_id "
    "WHERE tag.id IS NULL",
    "SELECT a.id FROM a LEFT JOIN tag ON a.id = tag.a_id JOIN big ON a.k = big.k",
    "SELECT a.id FROM a JOIN big ON a.k = big.k RIGHT JOIN tag ON a.id = tag.a_id",
    "SELECT a.id FROM a JOIN big ON a.k = big.k FULL JOIN tag ON a.id = tag.a_id",
    "SELECT a.id FROM a LEFT JOIN big ON a.k = big.k LEFT JOIN tag ON big.id = tag.a_id",
)


def test_an_inner_join_may_now_move_before_an_outer_one(wide: Database):
    """The milestone, in one query.

    ``LEFT JOIN tag ON a.id = tag.a_id`` reads only ``a``, so it does not need
    ``big`` on its left however it was written. Joining twenty rows to twenty
    rows and then to twenty thousand beats the other way round, and Milestone 18
    had no way to say so.
    """
    sql = REORDERABLE[0]
    assert order_of(wide, sql) == "a LEFT tag x big"
    assert "could reorder around them" in explain(wide, sql)


def test_the_reordering_is_worth_measuring(wide: Database, monkeypatch):
    """Not merely a different plan: a cheaper one, by the model's own numbers."""
    sql = REORDERABLE[0]
    freed = plan(wide, sql).estimated_cost

    syntactic_only(monkeypatch)
    pinned = plan(wide, sql).estimated_cost
    assert order_of(wide, sql) == "a x big LEFT tag"
    assert freed < pinned


@pytest.mark.parametrize("sql", REORDERABLE, ids=range(len(REORDERABLE)))
def test_reordering_does_not_change_the_answer(
    wide: Database, monkeypatch: pytest.MonkeyPatch, sql: str
):
    """The contract, checked against the planner it replaced rather than asserted.

    Every query is planned twice: once with Milestone 18's rule that an outer
    join needs everything written to its left, and once with this milestone's.
    A reordering that changes a row is not an optimisation.
    """
    freed = sorted(rows(wide, sql))
    syntactic_only(monkeypatch)
    assert sorted(rows(wide, sql)) == freed, f"the reordering changed:\n{sql}"


def test_a_right_join_gets_no_freedom(wide: Database):
    """And this is not conservatism, it is the identity failing.

    A ``RIGHT`` join NULL-extends everything accumulated to its left rather than
    the table arriving, so an inner join moved below one would be handed NULLs
    the written query never showed it. ``min_left`` stays syntactic for
    ``RIGHT`` and ``FULL``, and the order is pinned exactly as Milestone 18 had
    it.
    """
    for kind in ("RIGHT", "FULL"):
        sql = (
            f"SELECT a.id FROM a JOIN big ON a.k = big.k {kind} JOIN tag ON a.id = tag.a_id"
        )
        assert order_of(wide, sql).endswith(f"{kind} tag"), order_of(wide, sql)
        assert "needed everything written to its left" in explain(wide, sql)


def test_an_outer_join_that_reads_every_table_still_pins_the_order(wide: Database):
    # `min_left` is a proof, not a preference. An ON that reads both tables to
    # its left proves nothing can be missing, and the order is as written.
    sql = (
        "SELECT a.id FROM a JOIN big ON a.k = big.k "
        "LEFT JOIN tag ON a.id = tag.a_id AND big.id = tag.id"
    )
    assert order_of(wide, sql) == "a x big LEFT tag"
    assert "needed everything written to its left" in explain(wide, sql)


def test_half_an_outer_join_is_not_a_relation(chain: Database):
    """The validity rule, which is what stops the search inventing a subplan.

    ``b`` exists NULL-extended only because the join that made it ran, and that
    join needed ``a``. So ``{b, c}`` is not a set of tables any plan could hold,
    and the search must never cost one. Left unchecked it would build ``b ⨝ c``
    and then have nowhere to put the outer join.
    """
    # The later ON reads only `a` and `c`, so Milestone 19 leaves the LEFT join
    # alone and the validity rule is what does the work here.
    sql = (
        "SELECT a.id, b.id, c.id FROM a "
        "LEFT JOIN b ON a.id = b.ref "
        "JOIN c ON a.n = c.ref ORDER BY a.id, b.id;"
    )
    order = order_of(chain, sql)
    assert order.index("a") < order.index("b"), (
        f"`b` may not appear before the join that produced it, got {order}"
    )
    assert rows(chain, sql) == [(1, 100, 200), (1, 101, 200), (2, 102, 201)]
