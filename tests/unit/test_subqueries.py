"""Subqueries, and the one shape of them this milestone implements.

An **uncorrelated** subquery names nothing outside itself. It therefore depends
on no row of the query around it, has one value for the whole statement however
many rows that statement scans, and can be run once. That is not an
optimisation of some more general mechanism; for this shape it is the entire
semantics, which is why folding happens before binding and nothing downstream
learns that subqueries exist.

A **correlated** subquery is a different feature. It is refused by name here
rather than executed per row, because a query that silently becomes one
execution per outer row is a plan nobody would have chosen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.errors import BindingError, UnsupportedSqlError
from engine.executor.engine import execute_script
from engine.parser.ast import ScalarSubquery, walk
from engine.parser.parser import parse_statement
from engine.planner.physical import PhysicalIndexScan, walk_physical

ORDERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("total", DataType.INTEGER),
    Column("city", DataType.TEXT),
)
USERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("city", DataType.TEXT),
)

ORDER_ROWS = [
    (1, 100, "london"),
    (2, 300, "london"),
    (3, 200, "ny"),
    (4, None, "ny"),
]


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "sub.chendb", page_size=4096) as handle:
        handle.create_table("orders", ORDERS)
        handle.create_table("users", USERS)
        handle.insert_many("orders", ORDER_ROWS)
        handle.insert_many("users", [(1, "london"), (2, "ny")])
        yield handle


def rows(db: Database, sql: str):
    return list(execute_script(sql, db)[-1].rows)


def explain(db: Database, sql: str) -> str:
    return "\n".join(row[0] for row in execute_script(f"EXPLAIN {sql}", db)[-1].rows)


# -- parsing ------------------------------------------------------------------


def test_a_subquery_parses_wherever_a_value_may_appear():
    """The grammar recurses into a whole statement in exactly one place.

    Worth pinning at every position rather than one, because each is a
    different call into ``_primary`` and a parser can easily support the
    obvious one and not the rest.
    """
    for sql in (
        "SELECT (SELECT MAX(total) FROM orders) FROM users",
        "SELECT id FROM users WHERE id = (SELECT MIN(id) FROM orders)",
        "SELECT id FROM users WHERE id > (SELECT MIN(id) FROM orders) + 1",
        "SELECT id FROM users GROUP BY id HAVING COUNT(*) > (SELECT 0 FROM orders)",
        "SELECT id FROM users u JOIN orders o ON o.id = (SELECT MIN(id) FROM orders)",
    ):
        statement = parse_statement(sql)
        assert any(isinstance(node, ScalarSubquery) for node in walk(statement)), sql


def test_the_subquery_keeps_its_own_span():
    # The node covers the brackets, so selecting it in the UI highlights
    # `(SELECT …)` rather than the statement inside it.
    sql = "SELECT id FROM users WHERE id = (SELECT MIN(id) FROM orders)"
    subquery = next(
        node for node in walk(parse_statement(sql)) if isinstance(node, ScalarSubquery)
    )
    assert sql[subquery.span.start : subquery.span.end] == "(SELECT MIN(id) FROM orders)"


# -- what it computes ---------------------------------------------------------


def test_a_subquery_in_a_where_is_a_constant(db: Database):
    # AVG over 100, 300, 200 and a NULL is 200, and only one order beats it.
    assert rows(
        db, "SELECT id FROM orders WHERE total > (SELECT AVG(total) FROM orders)"
    ) == [(2,)]


def test_a_subquery_in_the_select_list_repeats_per_row(db: Database):
    assert rows(
        db, "SELECT id, (SELECT MAX(total) FROM orders) FROM orders ORDER BY id"
    ) == [(1, 300), (2, 300), (3, 300), (4, 300)]


def test_no_rows_is_null_rather_than_an_error(db: Database):
    """And this is what makes the empty case usable rather than fatal.

    ``x = NULL`` is UNKNOWN for every row, so the query returns nothing, which
    is the answer. PostgreSQL does the same; an error here would mean a
    perfectly reasonable query failing because a table happened to be empty.
    """
    assert (
        rows(db, "SELECT id FROM orders WHERE id = (SELECT id FROM orders WHERE id = 99)")
        == []
    )
    assert rows(db, "SELECT (SELECT id FROM orders WHERE id = 99) FROM users") == [
        (None,),
        (None,),
    ]


def test_a_subquery_composes_with_arithmetic_and_logic(db: Database):
    assert rows(
        db,
        "SELECT id FROM orders "
        "WHERE total >= (SELECT MIN(total) FROM orders) + 100 "
        "AND city = (SELECT city FROM users WHERE id = 1) ORDER BY id",
    ) == [(2,)]


def test_a_folded_subquery_can_reach_an_index(tmp_path: Path):
    """The reason folding happens before binding rather than at execution.

    Downstream sees an ordinary literal, so every part of the planner written
    before subqueries existed works on one unchanged: the index matcher only
    recognises ``column <op> literal``, and after folding that is what this is.
    """
    with Database.open(tmp_path / "indexed.chendb", page_size=4096) as db:
        db.create_table("orders", ORDERS)
        db.insert_many("orders", [(n, n, "x") for n in range(500)])
        db.create_index("orders_total", "orders", "total")
        db.analyze()

        sql = "SELECT id FROM orders WHERE total = (SELECT MAX(total) FROM orders)"
        assert rows(db, sql) == [(499,)]
        planned = execute_script(f"EXPLAIN {sql}", db)[-1].planned
        assert any(
            isinstance(node, PhysicalIndexScan) for node in walk_physical(planned.root)
        ), "a folded subquery is a literal, and a literal can use an index:\n" + explain(
            db, sql
        )


# -- what it refuses ----------------------------------------------------------


def test_more_than_one_row_is_an_error(db: Database):
    # No defensible answer exists. Taking the first would make the query depend
    # on physical order, which is the kind of wrong that looks right until a
    # VACUUM moves a page.
    with pytest.raises(BindingError, match="returned 4 rows"):
        rows(db, "SELECT id FROM orders WHERE total = (SELECT total FROM orders)")


def test_more_than_one_column_is_an_error(db: Database):
    with pytest.raises(BindingError, match="must return one column"):
        rows(db, "SELECT id FROM orders WHERE total = (SELECT id, total FROM orders)")


def test_a_star_is_not_one_column(db: Database):
    # `SELECT *` might be one column today and two after an ALTER TABLE, so it
    # is refused on shape rather than on the count it currently happens to have.
    with pytest.raises(BindingError, match="must return one column"):
        rows(db, "SELECT id FROM orders WHERE total = (SELECT * FROM users)")


def test_a_correlated_subquery_is_refused_by_name(db: Database):
    """Not left to fail as "no column named", which is what it would otherwise be.

    An unqualified column that does not exist inside the subquery is a typo. A
    reference qualified with a table from the query *outside* is a correlated
    subquery, and telling somebody their column does not exist when it plainly
    does is the kind of error message that costs an hour.
    """
    with pytest.raises(UnsupportedSqlError, match="correlated subquery"):
        rows(
            db,
            "SELECT o.id FROM orders o WHERE o.total = "
            "(SELECT MAX(i.total) FROM orders i WHERE i.city = o.city)",
        )


def test_a_subquery_over_its_own_alias_is_not_correlated(db: Database):
    # `orders` inside names the subquery's own FROM, not the outer one, even
    # though the outer query uses the same table. Refusing this would rule out
    # most of the useful cases.
    assert rows(
        db,
        "SELECT id FROM orders WHERE total = "
        "(SELECT MAX(orders.total) FROM orders) ORDER BY id",
    ) == [(2,)]


def test_a_genuinely_unknown_column_still_says_so(db: Database):
    with pytest.raises(BindingError, match="no column named"):
        rows(db, "SELECT id FROM orders WHERE total = (SELECT nope FROM users)")
