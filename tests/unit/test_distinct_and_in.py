"""``SELECT DISTINCT`` and ``IN``, and the NULL rules that make both surprising.

The two features are unrelated in the engine and inseparable in what they teach,
because each is a place where SQL's equality is not the one a programmer expects:

* ``DISTINCT`` treats two NULLs as **the same**, so one row survives, even
  though ``NULL = NULL`` is unknown everywhere else in the language.
* ``NOT IN`` treats a NULL in its list as poison, so ``x NOT IN (1, NULL)`` is
  never TRUE, however unrelated ``x`` is to either.

Both are the standard's, both are what PostgreSQL and SQLite do, and both are
checked against SQLite in ``tests/differential/`` on generated data as well.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.errors import EvaluationError, UnsupportedSqlError
from engine.executor.engine import execute_script
from engine.executor.operators import distinct_key
from engine.parser.ast import InList
from engine.parser.parser import parse_statement
from engine.planner.physical import PhysicalDistinct, PhysicalIndexScan, walk_physical

ROWS_SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("city", DataType.TEXT),
    Column("age", DataType.INTEGER),
    Column("ok", DataType.BOOLEAN),
)

ROWS = [
    (1, "london", 30, True),
    (2, "london", 30, True),
    (3, "ny", None, False),
    (4, "ny", None, False),
    (5, None, 40, None),
    (6, None, 40, None),
    (7, "london", 25, True),
]


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "d.chendb", page_size=4096) as handle:
        handle.create_table("t", ROWS_SCHEMA)
        handle.insert_many("t", ROWS)
        yield handle


def rows(db: Database, sql: str):
    return list(execute_script(sql, db)[-1].rows)


def plan(db: Database, sql: str):
    return execute_script(f"EXPLAIN {sql}", db)[-1].planned


# -- DISTINCT -----------------------------------------------------------------


def test_distinct_deduplicates_the_output_row(db: Database):
    assert rows(db, "SELECT DISTINCT city FROM t ORDER BY city") == [
        ("london",),
        ("ny",),
        (None,),
    ]


def test_distinct_is_over_the_whole_projection_not_each_column(db: Database):
    # `london` appears with two ages, so it appears twice. Deduplicating each
    # column separately would give a shorter answer and a wrong one.
    assert rows(db, "SELECT DISTINCT city, age FROM t ORDER BY city, age") == [
        ("london", 25),
        ("london", 30),
        ("ny", None),
        (None, 40),
    ]


def test_two_nulls_are_the_same_row_here_and_nowhere_else(db: Database):
    """The exception the standard writes out, and the reason it is worth a test.

    ``NULL = NULL`` is unknown, so a ``WHERE`` never matches one against
    another. ``DISTINCT`` uses *not distinct from* instead, under which two
    NULLs are the same and one survives. ``GROUP BY`` does this too, which is
    why the two agree below.
    """
    assert rows(db, "SELECT DISTINCT age FROM t ORDER BY age") == [
        (25,),
        (30,),
        (40,),
        (None,),
    ]
    assert rows(db, "SELECT age FROM t GROUP BY age ORDER BY age") == rows(
        db, "SELECT DISTINCT age FROM t ORDER BY age"
    )
    # And the contrast, in the same fixture: equality sees none of them.
    assert rows(db, "SELECT id FROM t WHERE age = age ORDER BY id") == [
        (1,),
        (2,),
        (5,),
        (6,),
        (7,),
    ]


def test_a_boolean_and_an_integer_are_different_rows(db: Database):
    """Python says ``True == 1`` and hashes them alike. SQL does not.

    A plain ``set`` of row tuples would fold a BOOLEAN row into an INTEGER one
    and silently lose it, and no test of the SQL surface would notice while the
    fixture happened to hold only one of the two types.
    """
    assert distinct_key((True,)) != distinct_key((1,))
    assert distinct_key((1,)) != distinct_key((1.0,))
    assert distinct_key((None,)) != distinct_key(("null",))
    # And two values that really are equal collapse, including this pair, whose
    # reprs differ while their values do not.
    assert distinct_key((-0.0,)) == distinct_key((0.0,))
    assert distinct_key(("a", None)) == distinct_key(("a", None))


def test_distinct_streams_rather_than_blocking(db: Database):
    """The reason it is its own operator rather than a sort that drops neighbours.

    A sort has to see every row before it emits one. This does not, so a
    ``LIMIT`` above it really does stop the scan early, and the plan says so by
    putting the limit above a distinct above a scan with no sort in between.
    """
    nodes = [
        node.node_type
        for node in walk_physical(plan(db, "SELECT DISTINCT city FROM t LIMIT 2").root)
    ]
    assert "PhysicalDistinct" in nodes
    assert "PhysicalSort" not in nodes
    assert len(rows(db, "SELECT DISTINCT city FROM t LIMIT 2")) == 2


def test_distinct_sits_above_the_projection(db: Database):
    # `SELECT DISTINCT city` over a table with a unique id must give three rows,
    # not seven, which is only true if the dedup happens after the id is dropped.
    root = plan(db, "SELECT DISTINCT city FROM t").root
    assert isinstance(root, PhysicalDistinct)
    assert root.child.node_type == "PhysicalProject"


def test_distinct_composes_with_grouping(db: Database):
    # Deduplicating an already-grouped result is redundant here and must still
    # be correct: the groups are distinct by construction, so nothing is lost.
    assert rows(db, "SELECT DISTINCT city FROM t GROUP BY city ORDER BY city") == rows(
        db, "SELECT city FROM t GROUP BY city ORDER BY city"
    )


# -- IN -----------------------------------------------------------------------


def test_in_is_a_union_of_equalities(db: Database):
    assert rows(db, "SELECT id FROM t WHERE age IN (30, 40) ORDER BY id") == [
        (1,),
        (2,),
        (5,),
        (6,),
    ]
    assert rows(db, "SELECT id FROM t WHERE city IN ('ny') ORDER BY id") == [(3,), (4,)]


def test_not_in_is_the_negation_of_that(db: Database):
    assert rows(db, "SELECT id FROM t WHERE age NOT IN (30) ORDER BY id") == [
        (5,),
        (6,),
        (7,),
    ]


def test_a_null_in_the_list_makes_not_in_impossible(db: Database):
    """The trap, and it catches everybody once.

    ``x NOT IN (1, NULL)`` means ``x <> 1 AND x <> NULL``. The second conjunct
    is unknown for every row, and ``TRUE AND unknown`` is unknown, so the whole
    predicate is never TRUE and the query returns nothing. No row is "wrong":
    the answer to "is x different from a value I do not know" is genuinely
    unknown.

    ``IN`` is unaffected, because ``TRUE OR unknown`` is TRUE.
    """
    assert rows(db, "SELECT id FROM t WHERE age NOT IN (30, NULL)") == []
    assert rows(db, "SELECT id FROM t WHERE age IN (30, NULL) ORDER BY id") == [
        (1,),
        (2,),
    ]


def test_a_null_operand_is_unknown_not_false(db: Database):
    # Rows 3 and 4 have a NULL age, so neither IN nor NOT IN admits them.
    assert rows(db, "SELECT id FROM t WHERE age IN (30, 40, 25) ORDER BY id") == [
        (1,),
        (2,),
        (5,),
        (6,),
        (7,),
    ]
    assert rows(db, "SELECT id FROM t WHERE age NOT IN (30, 40, 25)") == []


def test_in_type_checks_like_every_other_comparison(db: Database):
    # `id = 'a'` is refused, so `id IN ('a')` must be too. A list is not a
    # licence to compare a number against a string.
    with pytest.raises(EvaluationError, match="cannot compare"):
        rows(db, "SELECT id FROM t WHERE id IN ('a')")


def test_in_composes_with_everything_else(db: Database):
    assert rows(
        db,
        "SELECT DISTINCT city FROM t "
        "WHERE age IN (25 + 5, 40) AND id NOT IN (2) ORDER BY city",
    ) == [("london",), (None,)]


def test_in_survives_as_its_own_node(db: Database):
    """Not desugared into ``OR`` at parse time, though the desugaring is exact.

    The AST view is meant to show the query somebody wrote. An error span
    pointing at an ``OR`` nobody typed costs more than the evaluator case this
    node needs.
    """
    statement = parse_statement("SELECT id FROM t WHERE age IN (1, 2)")
    assert isinstance(statement.where, InList)
    assert len(statement.where.items) == 2
    assert "IN (1, 2)" in "\n".join(
        row[0]
        for row in execute_script("EXPLAIN SELECT id FROM t WHERE age IN (1, 2)", db)[
            -1
        ].rows
    )


def test_a_subquery_in_the_list_is_refused_by_name(db: Database):
    # `IN (SELECT …)` is a semi-join, not a list. Evaluating it as one would
    # mean materialising the subquery per row, which is a plan nobody would
    # choose, so the parser says which of the two forms it understands.
    with pytest.raises(UnsupportedSqlError, match=r"IN \(SELECT"):
        parse_statement("SELECT id FROM t WHERE id IN (SELECT id FROM t)")


def test_not_in_is_not_confused_with_a_negated_expression(db: Database):
    # `NOT` is a prefix operator everywhere else, so the parser needs one token
    # of lookahead to tell these apart. Both must parse, and differently.
    assert isinstance(
        parse_statement("SELECT id FROM t WHERE age NOT IN (1)").where, InList
    )
    assert not isinstance(
        parse_statement("SELECT id FROM t WHERE NOT (age = 1)").where, InList
    )


# -- what the planner makes of them -------------------------------------------


def test_in_is_estimated_from_the_most_common_values(tmp_path: Path):
    """Each branch of the union is an equality, and Milestone 20 counts those.

    So on a column small enough for its most-common list to be complete, which
    is most of them, an ``IN`` estimate is exact rather than a guess. Before
    this milestone the predicate did not parse; the fallback it would have hit
    is a third of the table.
    """
    with Database.open(tmp_path / "e.chendb", page_size=4096) as db:
        db.create_table("t", ROWS_SCHEMA)
        db.insert_many(
            "t", [(n, ["london", "ny", "oslo"][n % 3], n % 50, True) for n in range(500)]
        )
        db.analyze()

        for sql, expected in (
            ("SELECT id FROM t WHERE city IN ('london', 'ny')", 334),
            ("SELECT id FROM t WHERE city NOT IN ('london')", 333),
            ("SELECT id FROM t WHERE age IN (7)", 10),
        ):
            assert len(rows(db, sql)) == expected
            assert plan(db, sql).estimated_rows == pytest.approx(expected, rel=0.02), sql


def test_an_in_predicate_can_prove_an_outer_join_is_inner(tmp_path: Path):
    """Milestone 19's analysis had to learn one more node, and this is the check.

    ``b.id IN (1, 2)`` cannot be TRUE about a row the join invented, for the
    same reason ``b.id = 1`` cannot: every branch of the union is unknown. An
    analysis that returned "could be anything" for an unrecognised node would
    be *safe* here and would quietly stop rewriting.
    """
    with Database.open(tmp_path / "j.chendb", page_size=4096) as db:
        db.create_table("t", ROWS_SCHEMA)
        db.create_table(
            "b",
            Schema.of(
                Column("id", DataType.INTEGER, nullable=False, primary_key=True),
                Column("t_id", DataType.INTEGER),
            ),
        )
        db.insert_many("t", ROWS)
        db.insert_many("b", [(1, 1), (2, 2)])

        sql = "SELECT t.id FROM t LEFT JOIN b ON t.id = b.t_id WHERE b.id IN (1, 2)"
        assert "simplify_outer_joins" in plan(db, sql).rewrites
        assert rows(db, sql) == [(1,), (2,)]

        # And the anti-join idiom still is not rewritten, because `IS NULL` is
        # TRUE about exactly those rows whatever else the WHERE says.
        kept = "SELECT t.id FROM t LEFT JOIN b ON t.id = b.t_id WHERE b.id IS NULL"
        assert "simplify_outer_joins" not in plan(db, kept).rewrites


def test_a_folded_in_list_can_still_reach_an_index(tmp_path: Path):
    # One equality is enough for the index planner, and `IN (x)` is one. More
    # than one would need a multi-range scan, which this engine has no operator
    # for, so it falls back to a filter and says so rather than pretending.
    with Database.open(tmp_path / "i.chendb", page_size=4096) as db:
        db.create_table("t", ROWS_SCHEMA)
        db.insert_many("t", [(n, "x", n, True) for n in range(500)])
        db.create_index("t_age", "t", "age")
        db.analyze()

        single = walk_physical(plan(db, "SELECT id FROM t WHERE age IN (7)").root)
        assert not any(isinstance(node, PhysicalIndexScan) for node in single), (
            "an IN list is not yet an index condition, and the plan should say so"
        )
        assert rows(db, "SELECT id FROM t WHERE age IN (7)") == [(7,)]
