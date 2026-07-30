"""Joins and aggregation.

Twelve milestones of ``SELECT`` meant one table, filtered and projected. The
planner chose between two access paths and the cost model, calibrated by
measurement in Milestone 6, had never had to make a decision it could plausibly
get wrong.

Joins change that twice over: there is more than one algorithm, and there is
more than one *order*. The tests are grouped accordingly — what the rows are,
then what the planner did to produce them — because the two fail for completely
different reasons and a mixed suite would not say which.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.errors import BindingError
from engine.executor.engine import execute_script
from engine.planner.physical import (
    PhysicalHashJoin,
    PhysicalIndexScan,
    PhysicalNestedLoopJoin,
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
    # COUNT is 0; everything else is NULL. Not zero — SUM over nothing has no
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
    exists. Pushed to the scan it becomes an access-path decision again — and
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
    ``small`` first anyway — its estimate is a twentieth of the size, so it is
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
# an orphan (user 99) and a NULL key — so a LEFT join has something to preserve,
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
    assert row == ("edsger", None, None, None), "not just the key — the whole side"


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
    survives regardless — the ones whose orders were all too small come back
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
    TRUE — so it rejects every preserved row and the outer join collapses to an
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
    # No key to hash, so the hash join is not a candidate at all — and the
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
    # And the reason for the asymmetry: pushed below and consumed, this would
    # leave the NULL-extended rows in the result, which the WHERE must reject.
    outer = explain(
        db,
        "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE o.total > 200;",
    )
    lines = outer.splitlines()
    filter_at = next(i for i, line in enumerate(lines) if "Filter" in line)
    join_at = next(i for i, line in enumerate(lines) if "Join" in line)
    assert filter_at < join_at, "the filter belongs above the join:\n" + outer


def test_explain_names_the_join_flavour(db: Database):
    # A plan display that showed an outer join as an inner one would be describing
    # something that is not happening — and join order is exactly what an outer
    # join constrains.
    outer = explain(db, "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id;")
    assert "LEFT" in outer
    assert "may not reorder across it" in outer

    inner = explain(db, "SELECT * FROM users u JOIN orders o ON u.id = o.user_id;")
    assert "LEFT" not in inner
    assert "may not reorder across it" not in inner


def test_an_outer_join_is_estimated_above_its_preserved_input(db: Database):
    """An outer join has a floor an inner join does not.

    Every preserved row appears whether it matched or not, so an estimate below
    the preserved side's row count is not merely imprecise — it is impossible, and
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

    It used to read positions into the shared conjunct pool — which an outer join
    contributes nothing to — so an outer join's equality was invisible to it and
    every one was costed as a full cross product.
    """
    outer = plan(db, "SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id;")
    join = next(
        node
        for node in walk_physical(outer.root)
        if isinstance(node, PhysicalHashJoin | PhysicalNestedLoopJoin)
    )
    assert join.estimated.rows < len(USER_ROWS) * len(ORDER_ROWS)
