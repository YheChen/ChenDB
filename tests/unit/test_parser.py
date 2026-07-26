"""Recursive-descent parser tests."""

from __future__ import annotations

import pytest

from engine.errors import ParseError, UnsupportedSqlError
from engine.parser.analyze import analyze_sql
from engine.parser.ast import (
    BinaryOp,
    BinaryOperator,
    ColumnConstraint,
    ColumnRef,
    CreateTableStatement,
    InsertStatement,
    IsNullTest,
    Literal,
    SelectStatement,
    Star,
    UnaryOp,
    UnaryOperator,
    walk,
)
from engine.parser.parser import MAX_EXPRESSION_DEPTH, parse, parse_statement
from engine.serialization.types import DataType


def select(sql: str) -> SelectStatement:
    statement = parse_statement(sql)
    assert isinstance(statement, SelectStatement)
    return statement


def where(sql: str):
    """The WHERE expression of a minimal SELECT wrapping ``sql``."""
    statement = select(f"SELECT * FROM t WHERE {sql}")
    assert statement.where is not None
    return statement.where


# -- SELECT ----------------------------------------------------------------


def test_select_star():
    statement = select("SELECT * FROM users")
    assert statement.is_select_star
    assert isinstance(statement.projections[0].expression, Star)
    assert statement.table.name == "users"
    assert statement.where is None


def test_select_named_columns():
    statement = select("SELECT id, name, age FROM users")
    assert [item.output_name for item in statement.projections] == ["id", "name", "age"]


def test_select_with_aliases_both_spellings():
    statement = select("SELECT age AS years, name nickname FROM users")
    assert [item.output_name for item in statement.projections] == ["years", "nickname"]
    assert statement.projections[0].alias == "years"


def test_an_expression_projection_gets_a_fallback_name():
    statement = select("SELECT age * 2 FROM users")
    assert statement.projections[0].output_name == "binaryop"


def test_select_star_alongside_a_column():
    statement = select("SELECT *, id FROM users")
    assert len(statement.projections) == 2
    assert not statement.is_select_star


def test_qualified_column_reference():
    expression = where("users.age > 1")
    assert isinstance(expression, BinaryOp)
    left = expression.left
    assert isinstance(left, ColumnRef)
    assert left.table == "users"
    assert left.name == "age"
    assert left.qualified_name == "users.age"


def test_a_quoted_identifier_can_be_a_reserved_word():
    statement = select('SELECT "select" FROM "table"')
    column = statement.projections[0].expression
    assert isinstance(column, ColumnRef)
    assert column.name == "select"
    assert statement.table.name == "table"


# -- expression precedence -------------------------------------------------


def test_and_binds_tighter_than_or():
    # a OR b AND c  parses as  a OR (b AND c)
    expression = where("a = 1 OR b = 2 AND c = 3")
    assert isinstance(expression, BinaryOp)
    assert expression.operator is BinaryOperator.OR
    assert isinstance(expression.right, BinaryOp)
    assert expression.right.operator is BinaryOperator.AND


def test_comparison_binds_tighter_than_and():
    expression = where("a = 1 AND b = 2")
    assert isinstance(expression, BinaryOp)
    assert expression.operator is BinaryOperator.AND
    assert isinstance(expression.left, BinaryOp)
    assert expression.left.operator is BinaryOperator.EQ


def test_multiplication_binds_tighter_than_addition():
    expression = where("1 + 2 * 3 = 7")
    comparison = expression
    assert isinstance(comparison, BinaryOp)
    addition = comparison.left
    assert isinstance(addition, BinaryOp)
    assert addition.operator is BinaryOperator.ADD
    assert isinstance(addition.right, BinaryOp)
    assert addition.right.operator is BinaryOperator.MULTIPLY


def test_arithmetic_binds_tighter_than_comparison():
    expression = where("age + 1 > 18")
    assert isinstance(expression, BinaryOp)
    assert expression.operator is BinaryOperator.GT
    assert isinstance(expression.left, BinaryOp)
    assert expression.left.operator is BinaryOperator.ADD


def test_subtraction_is_left_associative():
    # 1 - 2 - 3 must be (1 - 2) - 3, not 1 - (2 - 3).
    expression = where("1 - 2 - 3 = 0")
    left = expression.left  # type: ignore[union-attr]
    assert isinstance(left, BinaryOp)
    assert left.operator is BinaryOperator.SUBTRACT
    assert isinstance(left.left, BinaryOp)
    assert left.left.operator is BinaryOperator.SUBTRACT
    assert isinstance(left.right, Literal)
    assert left.right.value == 3


def test_parentheses_override_precedence():
    expression = where("(a = 1 OR b = 2) AND c = 3")
    assert isinstance(expression, BinaryOp)
    assert expression.operator is BinaryOperator.AND
    assert isinstance(expression.left, BinaryOp)
    assert expression.left.operator is BinaryOperator.OR


def test_a_parenthesised_span_includes_the_brackets():
    sql = "SELECT * FROM t WHERE (a = 1) AND b = 2"
    statement = select(sql)
    assert isinstance(statement.where, BinaryOp)
    assert statement.where.left.text_in(sql) == "(a = 1)"


def test_not_binds_looser_than_comparison():
    # NOT a = 1  is  NOT (a = 1)
    expression = where("NOT a = 1")
    assert isinstance(expression, UnaryOp)
    assert expression.operator is UnaryOperator.NOT
    assert isinstance(expression.operand, BinaryOp)


def test_unary_minus_on_a_literal():
    expression = where("age = -5")
    right = expression.right  # type: ignore[union-attr]
    assert isinstance(right, UnaryOp)
    assert right.operator is UnaryOperator.NEGATE


def test_double_negation_nests():
    expression = where("NOT NOT a = 1")
    assert isinstance(expression, UnaryOp)
    assert isinstance(expression.operand, UnaryOp)


# -- IS NULL ---------------------------------------------------------------


def test_is_null_is_its_own_node_not_an_equality():
    # `x = NULL` is UNKNOWN for every input, including NULL. Keeping IS NULL a
    # distinct node makes conflating them impossible downstream.
    expression = where("age IS NULL")
    assert isinstance(expression, IsNullTest)
    assert expression.negated is False


def test_is_not_null():
    expression = where("age IS NOT NULL")
    assert isinstance(expression, IsNullTest)
    assert expression.negated is True


def test_equality_against_null_still_parses_as_a_comparison():
    expression = where("age = NULL")
    assert isinstance(expression, BinaryOp)
    assert isinstance(expression.right, Literal)
    assert expression.right.value is None


# -- literals --------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "value", "data_type"),
    [
        ("1", 1, DataType.INTEGER),
        ("1.5", 1.5, DataType.FLOAT),
        ("'text'", "text", DataType.TEXT),
        ("TRUE", True, DataType.BOOLEAN),
        ("FALSE", False, DataType.BOOLEAN),
        ("NULL", None, None),
    ],
)
def test_literal_types(sql: str, value: object, data_type: DataType | None):
    expression = where(f"a = {sql}")
    right = expression.right  # type: ignore[union-attr]
    assert isinstance(right, Literal)
    assert right.value == value
    assert right.data_type is data_type


def test_null_has_no_type_until_it_is_bound():
    expression = where("a = NULL")
    right = expression.right  # type: ignore[union-attr]
    assert isinstance(right, Literal)
    # A typeless NULL is correct: its type comes from the column it is compared
    # against, which needs the catalog (Milestone 4).
    assert right.data_type is None


# -- CREATE TABLE ----------------------------------------------------------


def test_create_table():
    statement = parse_statement(
        "CREATE TABLE users ("
        "  id INTEGER PRIMARY KEY,"
        "  email TEXT NOT NULL,"
        "  age INTEGER,"
        "  active BOOLEAN"
        ")"
    )
    assert isinstance(statement, CreateTableStatement)
    assert statement.table.name == "users"
    assert [column.name for column in statement.columns] == [
        "id",
        "email",
        "age",
        "active",
    ]
    assert statement.columns[0].primary_key
    assert statement.columns[0].not_null  # implied by PRIMARY KEY
    assert statement.columns[1].not_null
    assert not statement.columns[2].not_null


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("INTEGER", DataType.INTEGER),
        ("INT", DataType.INTEGER),
        ("BIGINT", DataType.INTEGER),
        ("FLOAT", DataType.FLOAT),
        ("REAL", DataType.FLOAT),
        ("DOUBLE", DataType.FLOAT),
        ("BOOLEAN", DataType.BOOLEAN),
        ("BOOL", DataType.BOOLEAN),
        ("TEXT", DataType.TEXT),
        ("VARCHAR", DataType.TEXT),
    ],
)
def test_type_spellings(spelling: str, expected: DataType):
    statement = parse_statement(f"CREATE TABLE t (c {spelling})")
    assert isinstance(statement, CreateTableStatement)
    assert statement.columns[0].data_type is expected


def test_varchar_length_is_accepted_and_ignored():
    # TEXT is variable-width already, so a declared maximum would be a
    # constraint, not a layout decision. Accepting it keeps real DDL parseable.
    statement = parse_statement("CREATE TABLE t (name VARCHAR(255))")
    assert isinstance(statement, CreateTableStatement)
    assert statement.columns[0].data_type is DataType.TEXT


def test_if_not_exists():
    statement = parse_statement("CREATE TABLE IF NOT EXISTS t (c INTEGER)")
    assert isinstance(statement, CreateTableStatement)
    assert statement.if_not_exists


def test_explicit_null_constraint():
    statement = parse_statement("CREATE TABLE t (c INTEGER NULL)")
    assert isinstance(statement, CreateTableStatement)
    assert ColumnConstraint.NULL in statement.columns[0].constraints
    assert not statement.columns[0].not_null


def test_null_and_not_null_together_is_rejected():
    with pytest.raises(ParseError, match="cannot be both"):
        parse_statement("CREATE TABLE t (c INTEGER NULL NOT NULL)")


def test_a_duplicate_constraint_is_rejected():
    with pytest.raises(ParseError, match="duplicate constraint"):
        parse_statement("CREATE TABLE t (c INTEGER NOT NULL NOT NULL)")


def test_a_missing_type_names_the_alternatives():
    with pytest.raises(ParseError, match="expected a column type") as info:
        parse_statement("CREATE TABLE t (c)")
    assert "INTEGER" in info.value.expected


def test_a_table_with_no_columns_is_rejected():
    with pytest.raises(ParseError):
        parse_statement("CREATE TABLE t ()")


# -- INSERT ----------------------------------------------------------------


def test_insert_with_a_column_list():
    statement = parse_statement("INSERT INTO users (id, name) VALUES (1, 'Ada')")
    assert isinstance(statement, InsertStatement)
    assert statement.table.name == "users"
    assert statement.columns == ("id", "name")
    assert len(statement.rows) == 1
    assert statement.rows[0].width == 2


def test_insert_without_a_column_list_means_all_columns():
    statement = parse_statement("INSERT INTO users VALUES (1, 'Ada')")
    assert isinstance(statement, InsertStatement)
    assert statement.columns is None


def test_insert_multiple_rows():
    statement = parse_statement("INSERT INTO t VALUES (1), (2), (3)")
    assert isinstance(statement, InsertStatement)
    assert len(statement.rows) == 3


def test_insert_accepts_expressions_as_values():
    statement = parse_statement("INSERT INTO t VALUES (1 + 2, -3, NULL)")
    assert isinstance(statement, InsertStatement)
    assert isinstance(statement.rows[0].values[0], BinaryOp)
    assert isinstance(statement.rows[0].values[1], UnaryOp)


def test_a_row_that_does_not_match_the_column_list_is_rejected():
    with pytest.raises(ParseError, match="2 values but 3 columns"):
        parse_statement("INSERT INTO t (a, b, c) VALUES (1, 2)")


def test_rows_of_differing_widths_are_rejected():
    with pytest.raises(ParseError, match="same number of values"):
        parse_statement("INSERT INTO t VALUES (1, 2), (3)")


# -- scripts ---------------------------------------------------------------


def test_multiple_statements():
    statements = parse(
        "CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (1); SELECT * FROM t"
    )
    assert [statement.node_type for statement in statements] == [
        "CreateTableStatement",
        "InsertStatement",
        "SelectStatement",
    ]


def test_a_trailing_semicolon_is_optional():
    assert len(parse("SELECT * FROM t")) == 1
    assert len(parse("SELECT * FROM t;")) == 1
    assert len(parse("SELECT * FROM t;;;")) == 1


def test_an_empty_script_parses_to_nothing():
    assert parse("") == []
    assert parse("   \n -- just a comment \n ") == []


def test_a_missing_separator_between_statements_is_reported():
    with pytest.raises(ParseError, match="expected ';'"):
        parse("SELECT * FROM t SELECT * FROM t")


def test_parse_statement_rejects_two_statements():
    with pytest.raises(ParseError, match="exactly one statement"):
        parse_statement("SELECT 1 FROM t; SELECT 2 FROM t")


# -- node identity and spans ----------------------------------------------


def test_node_ids_are_unique_across_a_whole_script():
    statements = parse("SELECT a FROM t; SELECT b FROM u")
    ids = [node.node_id for statement in statements for node in walk(statement)]
    assert len(ids) == len(set(ids))


def test_every_node_span_slices_to_valid_source():
    sql = "SELECT name, age * 2 AS d FROM users WHERE age >= 18 AND name IS NOT NULL"
    statement = select(sql)
    for node in walk(statement):
        fragment = node.text_in(sql)
        assert fragment
        assert fragment.strip() == fragment or node is statement


def test_a_parent_span_contains_every_child_span():
    sql = "SELECT a, b FROM t WHERE x = 1 AND y = 2"
    for node in walk(select(sql)):
        for child in node.children():
            assert node.span.start <= child.span.start
            assert child.span.end <= node.span.end


def test_the_statement_span_covers_the_whole_statement():
    sql = "SELECT name FROM users WHERE age >= 18"
    assert select(sql).text_in(sql) == sql


def test_children_and_attributes_are_disjoint():
    for node in walk(select("SELECT a FROM t WHERE b = 1")):
        assert not (set(node.attributes()) & {"left", "right", "operand", "expression"})


# -- errors ----------------------------------------------------------------


def test_a_reserved_word_as_a_name_suggests_quoting_it():
    with pytest.raises(ParseError, match="reserved word") as info:
        parse_statement("SELECT * FROM order")
    assert "quote it" in str(info.value)


def test_an_error_carries_a_usable_position():
    with pytest.raises(ParseError) as info:
        parse_statement("SELECT * FROM")
    error = info.value
    assert error.start == 13
    assert error.line == 1
    assert error.column == 14
    assert "table name" in error.message


def test_the_error_says_what_it_expected():
    with pytest.raises(ParseError) as info:
        parse_statement("SELECT * users")
    assert "FROM" in info.value.expected


def test_star_inside_an_expression_is_explained():
    with pytest.raises(ParseError, match="only valid in a projection"):
        parse_statement("SELECT * FROM t WHERE * = 1")


@pytest.mark.parametrize(
    ("sql", "match"),
    [
        ("SELECT * FROM t ORDER BY a", "ORDER BY"),
        ("SELECT * FROM t LIMIT 10", "LIMIT"),
        ("SELECT * FROM t GROUP BY a", "GROUP BY"),
        ("SELECT DISTINCT a FROM t", "DISTINCT"),
        ("DELETE FROM t", "DELETE"),
        ("UPDATE t SET a = 1", "UPDATE"),
        ("DROP TABLE t", "DROP"),
        ("BEGIN", "Milestone 8"),
        ("EXPLAIN SELECT * FROM t", "Milestone 6"),
        ("CREATE INDEX i ON t (a)", "Milestone 5"),
        ("SELECT * FROM t WHERE a IN (1)", "IN"),
        ("SELECT * FROM t WHERE a LIKE 'x'", "LIKE"),
        ("CREATE TABLE t (a INTEGER UNIQUE)", "Milestone 5"),
        ("INSERT INTO t SELECT * FROM u", "Milestone 3"),
    ],
)
def test_valid_sql_that_is_not_implemented_says_so(sql: str, match: str):
    # "you wrote this wrong" and "ChenDB cannot do this yet" are different
    # messages, and only the second should point at a milestone.
    with pytest.raises(UnsupportedSqlError, match=match):
        parse_statement(sql)


def test_unsupported_is_a_kind_of_parse_error():
    # So a caller that only cares "the SQL did not work" can catch one type.
    with pytest.raises(ParseError):
        parse_statement("DELETE FROM t")


def test_deeply_nested_parentheses_fail_cleanly_instead_of_crashing():
    depth = MAX_EXPRESSION_DEPTH + 10
    sql = f"SELECT * FROM t WHERE {'(' * depth}1{')' * depth} = 1"
    with pytest.raises(ParseError, match="nested deeper"):
        parse_statement(sql)


def test_nesting_just_inside_the_limit_still_works():
    depth = MAX_EXPRESSION_DEPTH - 5
    sql = f"SELECT * FROM t WHERE {'(' * depth}1{')' * depth} = 1"
    assert select(sql).where is not None


# -- analyze_sql (the editor entry point) ---------------------------------


def test_analyze_returns_partial_results_for_incomplete_sql():
    outcome = analyze_sql("SELECT name FROM")
    assert not outcome.ok
    assert len(outcome.tokens) == 4  # SELECT name FROM EOF — all scanned fine
    assert outcome.statements == ()
    assert outcome.lexed_ok
    assert outcome.error is not None


def test_analyze_reports_a_lex_failure_separately():
    outcome = analyze_sql("SELECT 'unterminated")
    assert not outcome.lexed_ok
    assert outcome.tokens == ()
    assert outcome.error is not None
    assert "unterminated" in outcome.error.message


def test_analyze_never_raises():
    for sql in ["", "SELECT", "!!!", "SELECT * FROM", "'", "((((", "\x00"]:
        outcome = analyze_sql(sql)
        assert isinstance(outcome.ok, bool)


def test_analyze_succeeds_on_a_whole_script():
    outcome = analyze_sql("CREATE TABLE t (a INT); INSERT INTO t VALUES (1)")
    assert outcome.ok
    assert outcome.statement_kinds == ("CreateTableStatement", "InsertStatement")
    assert outcome.node_count > 0
    assert outcome.duration_ns > 0


def test_values_rows_are_nodes_so_their_expressions_are_reachable():
    # Regression: when `rows` held bare tuples of expressions, the generic tree
    # walk flattened only one level and every inserted value was invisible.
    statement = parse_statement("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
    literals = [node for node in walk(statement) if isinstance(node, Literal)]
    assert len(literals) == 4
    assert {literal.value for literal in literals} == {1, "x", 2, "y"}


def test_a_values_row_span_covers_its_brackets():
    sql = "INSERT INTO t VALUES (1, 'x')"
    statement = parse_statement(sql)
    assert isinstance(statement, InsertStatement)
    assert statement.rows[0].text_in(sql) == "(1, 'x')"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, b FROM t WHERE x = 1 AND y IS NULL",
        "CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT NOT NULL)",
        "INSERT INTO t (a, b) VALUES (1, 'x'), (2 + 3, NULL)",
    ],
)
def test_no_node_hides_children_inside_a_nested_tuple(sql: str):
    """Every field must be a scalar, a Node, or a flat tuple of one of those.

    A tuple of tuples would make `children()` skip a whole level, which is
    exactly the bug `ValuesRow` was introduced to fix. This guards the invariant
    for every node type at once.
    """
    import dataclasses

    for node in walk(parse_statement(sql)):
        for field in dataclasses.fields(node):
            value = getattr(node, field.name)
            if isinstance(value, tuple):
                assert not any(isinstance(item, tuple) for item in value), (
                    f"{node.node_type}.{field.name} nests tuples"
                )
