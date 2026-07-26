"""Expression evaluation and SQL's three-valued logic.

The truth tables here are not a stylistic choice — they are what SQL requires,
and getting them wrong produces silently wrong answers rather than errors. That
makes them worth pinning down exhaustively.
"""

from __future__ import annotations

import pytest

from engine.errors import BindingError, EvaluationError
from engine.executor.binder import bind_expression
from engine.executor.expression import describe_expression, evaluate, is_true
from engine.parser.parser import parse_statement
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False),
    Column("name", DataType.TEXT),
    Column("age", DataType.INTEGER),
    Column("active", DataType.BOOLEAN),
    Column("score", DataType.FLOAT),
)

#: id=1, name='Ada', age=NULL, active=True, score=2.5
ROW = (1, "Ada", None, True, 2.5)


def value_of(sql: str, row=ROW):
    """Evaluate a WHERE expression against ``row``."""
    statement = parse_statement(f"SELECT * FROM t WHERE {sql}")
    assert statement.where is not None
    return evaluate(bind_expression(statement.where, SCHEMA), row)


# -- NULL propagation ------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "age = 1",
        "age <> 1",
        "age > 1",
        "age < 1",
        "age >= 1",
        "age <= 1",
        "age = NULL",
        "NULL = NULL",
        "age + 1 = 1",
        "age * 2 = 1",
        "-age = 1",
        "NOT age = 1",
    ],
)
def test_every_operator_propagates_null_as_unknown(sql: str):
    # NULL means *unknown*. Comparing against an unknown is unknown — never
    # true, and importantly never false.
    assert value_of(sql) is None


def test_null_equals_null_is_not_true():
    # The classic. `x = NULL` never matches, which is why IS NULL exists.
    assert value_of("NULL = NULL") is None
    assert value_of("age = age") is None


def test_is_null_is_the_only_thing_that_can_see_a_null():
    assert value_of("age IS NULL") is True
    assert value_of("age IS NOT NULL") is False
    assert value_of("name IS NULL") is False
    assert value_of("name IS NOT NULL") is True


def test_is_null_always_returns_a_boolean_never_null():
    for sql in ("age IS NULL", "age IS NOT NULL", "NULL IS NULL"):
        assert isinstance(value_of(sql), bool)


# -- AND / OR truth tables -------------------------------------------------

TRUE, FALSE, NULL = "id = 1", "id = 2", "age = 1"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (TRUE, TRUE, True),
        (TRUE, FALSE, False),
        (TRUE, NULL, None),
        (FALSE, TRUE, False),
        (FALSE, FALSE, False),
        (FALSE, NULL, False),  # FALSE AND unknown is FALSE: it cannot be true
        (NULL, TRUE, None),
        (NULL, FALSE, False),
        (NULL, NULL, None),
    ],
)
def test_and_truth_table(left: str, right: str, expected: bool | None):
    assert value_of(f"({left}) AND ({right})") is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (TRUE, TRUE, True),
        (TRUE, FALSE, True),
        (TRUE, NULL, True),  # TRUE OR unknown is TRUE: it cannot be false
        (FALSE, TRUE, True),
        (FALSE, FALSE, False),
        (FALSE, NULL, None),
        (NULL, TRUE, True),
        (NULL, FALSE, None),
        (NULL, NULL, None),
    ],
)
def test_or_truth_table(left: str, right: str, expected: bool | None):
    assert value_of(f"({left}) OR ({right})") is expected


def test_not_of_unknown_stays_unknown():
    assert value_of(f"NOT ({NULL})") is None
    assert value_of(f"NOT ({TRUE})") is False
    assert value_of(f"NOT ({FALSE})") is True


def test_only_exactly_true_passes_a_where_clause():
    # The consequence of the tables above, and the reason `WHERE age > 18`
    # silently drops rows with a NULL age.
    assert is_true(True) is True
    assert is_true(False) is False
    assert is_true(None) is False
    assert is_true(1) is False, "a truthy non-boolean must not pass"


# -- arithmetic ------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("id + 1", 2),
        ("id - 3", -2),
        ("id * 4", 4),
        ("score * 2", 5.0),
        ("score + id", 3.5),
        ("7 / 2", 3),
        ("-7 / 2", -3),
        ("7 / -2", -3),
        ("7 % 3", 1),
        ("-7 % 3", -1),
        ("score / 2", 1.25),
    ],
)
def test_arithmetic(sql: str, expected: object):
    assert value_of(f"({sql}) = ({sql})") is True
    statement = parse_statement(f"SELECT {sql} FROM t")
    bound = bind_expression(statement.projections[0].expression, SCHEMA)
    assert evaluate(bound, ROW) == expected


def test_integer_division_truncates_toward_zero():
    # C and PostgreSQL semantics, not Python's floor division. -7 / 2 is -3,
    # not -4, and the sign of a modulo follows the dividend.
    assert value_of("(-7 / 2) = -3") is True
    assert value_of("(-7 % 3) = -1") is True


def test_division_by_zero_raises_rather_than_returning_null():
    # PostgreSQL raises; SQLite returns NULL. Raising never hides a bug in the
    # query behind a row that quietly vanishes from the result.
    with pytest.raises(EvaluationError, match="division by zero"):
        value_of("id / 0 = 1")
    with pytest.raises(EvaluationError, match="modulo by zero"):
        value_of("id % 0 = 1")


def test_null_divided_by_zero_is_still_null():
    # NULL propagation happens before the operator runs, so there is nothing to
    # divide and nothing to raise about.
    assert value_of("age / 0 = 1") is None


# -- type discipline -------------------------------------------------------


def test_comparing_text_with_a_number_is_refused():
    # Python would happily order "10" < "9" lexicographically. SQL requires an
    # explicit cast, and a clear refusal beats a plausible wrong answer.
    with pytest.raises(EvaluationError, match="cannot compare"):
        value_of("name = 1")
    with pytest.raises(EvaluationError, match="cannot compare"):
        value_of("id < 'abc'")


def test_comparing_a_boolean_with_a_number_is_refused():
    with pytest.raises(EvaluationError, match="cannot compare"):
        value_of("active = 1")


def test_integers_and_floats_compare_freely():
    assert value_of("id < score") is True
    assert value_of("score > 1") is True


def test_arithmetic_on_text_is_refused():
    with pytest.raises(EvaluationError, match="numeric"):
        value_of("name + 1 = 1")


def test_logical_operators_need_booleans():
    with pytest.raises(EvaluationError, match="boolean"):
        value_of("id AND active")
    with pytest.raises(EvaluationError, match="boolean"):
        value_of("NOT id")


def test_a_boolean_column_is_a_predicate_on_its_own():
    assert value_of("active") is True
    assert value_of("NOT active") is False


# -- binding ---------------------------------------------------------------


def test_binding_resolves_a_column_to_its_index():
    statement = parse_statement("SELECT * FROM t WHERE age > 1")
    bound = bind_expression(statement.where, SCHEMA)
    assert bound.left.column_index == 2  # type: ignore[union-attr]
    assert bound.left.name == "age"  # type: ignore[union-attr]
    assert bound.left.data_type is DataType.INTEGER  # type: ignore[union-attr]


def test_binding_an_unknown_column_names_the_alternatives():
    statement = parse_statement("SELECT * FROM t WHERE nope > 1")
    with pytest.raises(BindingError, match="no column named 'nope'") as info:
        bind_expression(statement.where, SCHEMA)
    assert "age" in str(info.value)
    # The position must point at the identifier, not the whole statement.
    assert info.value.start == statement.where.left.span.start  # type: ignore[union-attr]


def test_binding_is_case_insensitive_but_keeps_the_declared_name():
    statement = parse_statement("SELECT * FROM t WHERE AGE > 1")
    bound = bind_expression(statement.where, SCHEMA)
    assert bound.left.name == "age"  # type: ignore[union-attr]


def test_binding_preserves_spans_so_errors_stay_locatable():
    statement = parse_statement("SELECT * FROM t WHERE age > 1 AND id = 2")
    bound = bind_expression(statement.where, SCHEMA)
    assert bound.span == statement.where.span


def test_a_bare_star_cannot_be_bound_as_an_expression():
    statement = parse_statement("SELECT * FROM t")
    with pytest.raises(BindingError, match="only valid on its own"):
        bind_expression(statement.projections[0].expression, SCHEMA)


# -- rendering -------------------------------------------------------------


def test_expressions_render_back_to_readable_sql():
    statement = parse_statement("SELECT * FROM t WHERE age >= 18 AND name IS NOT NULL")
    bound = bind_expression(statement.where, SCHEMA)
    # Binary operators parenthesise so precedence is unambiguous; IS NULL is
    # postfix and needs none.
    assert describe_expression(bound) == "((age >= 18) AND name IS NOT NULL)"


def test_rendering_quotes_text_and_names_null():
    statement = parse_statement("SELECT * FROM t WHERE name = 'x' OR age = NULL")
    rendered = describe_expression(bind_expression(statement.where, SCHEMA))
    assert "'x'" in rendered
    assert "NULL" in rendered
