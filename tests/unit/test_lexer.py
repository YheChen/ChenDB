"""Tokenizer tests."""

from __future__ import annotations

import itertools

import pytest

from engine.errors import LexError
from engine.parser.lexer import tokenize
from engine.parser.tokens import Keyword, TokenType


def types(sql: str) -> list[TokenType]:
    return [token.type for token in tokenize(sql)]


def lexemes(sql: str) -> list[str]:
    return [token.lexeme for token in tokenize(sql)[:-1]]  # drop EOF


# -- structure -------------------------------------------------------------


def test_the_stream_always_ends_with_eof():
    assert types("") == [TokenType.EOF]
    assert types("SELECT")[-1] is TokenType.EOF


def test_a_simple_select_tokenizes_as_expected():
    assert types("SELECT * FROM users") == [
        TokenType.KEYWORD,
        TokenType.STAR,
        TokenType.KEYWORD,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]


def test_keywords_are_case_insensitive_but_keep_their_source_text():
    tokens = tokenize("SeLeCt")
    assert tokens[0].type is TokenType.KEYWORD
    assert tokens[0].keyword is Keyword.SELECT
    # The original casing survives, so the editor can highlight what was typed.
    assert tokens[0].lexeme == "SeLeCt"


def test_identifiers_are_not_confused_with_keywords():
    tokens = tokenize("selected")
    assert tokens[0].type is TokenType.IDENTIFIER
    assert tokens[0].keyword is None


# -- spans -----------------------------------------------------------------


def test_every_token_span_slices_back_to_its_own_lexeme():
    sql = "SELECT name, age FROM users WHERE age >= 18"
    for token in tokenize(sql)[:-1]:
        assert token.span.text_in(sql) == token.lexeme


def test_spans_are_contiguous_and_non_overlapping():
    sql = "SELECT a,b FROM t"
    tokens = tokenize(sql)[:-1]
    for left, right in itertools.pairwise(tokens):
        assert left.span.end <= right.span.start


def test_line_and_column_track_newlines():
    tokens = tokenize("SELECT\n  name\nFROM t")
    by_lexeme = {token.lexeme: token.span for token in tokens[:-1]}
    assert by_lexeme["SELECT"].line == 1
    assert by_lexeme["SELECT"].column == 1
    assert by_lexeme["name"].line == 2
    assert by_lexeme["name"].column == 3
    assert by_lexeme["FROM"].line == 3


# -- literals --------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected_type", "expected_value"),
    [
        ("0", TokenType.INT_LITERAL, 0),
        ("42", TokenType.INT_LITERAL, 42),
        ("9223372036854775807", TokenType.INT_LITERAL, 2**63 - 1),
        ("1.5", TokenType.FLOAT_LITERAL, 1.5),
        ("0.25", TokenType.FLOAT_LITERAL, 0.25),
        ("1e3", TokenType.FLOAT_LITERAL, 1000.0),
        ("1E3", TokenType.FLOAT_LITERAL, 1000.0),
        ("1.5e-3", TokenType.FLOAT_LITERAL, 0.0015),
        ("2e+2", TokenType.FLOAT_LITERAL, 200.0),
    ],
)
def test_numbers(sql: str, expected_type: TokenType, expected_value: object):
    token = tokenize(sql)[0]
    assert token.type is expected_type
    assert token.value == expected_value


def test_a_trailing_dot_is_not_part_of_the_number():
    # `1.` then EOF: the dot is a separate token, because `t.c` needs it to be.
    assert types("1.") == [TokenType.INT_LITERAL, TokenType.DOT, TokenType.EOF]


def test_qualified_column_reference_splits_on_the_dot():
    assert types("users.age") == [
        TokenType.IDENTIFIER,
        TokenType.DOT,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]


def test_a_number_run_into_a_word_is_an_error_not_two_tokens():
    with pytest.raises(LexError, match="invalid number"):
        tokenize("123abc")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("''", ""),
        ("'hello'", "hello"),
        ("'it''s'", "it's"),
        ("''''", "'"),
        ("'with \"double\" quotes'", 'with "double" quotes'),
        ("'héllo 世界 🎉'", "héllo 世界 🎉"),
        ("'-- not a comment'", "-- not a comment"),
    ],
)
def test_strings(sql: str, expected: str):
    token = tokenize(sql)[0]
    assert token.type is TokenType.STRING_LITERAL
    assert token.value == expected


def test_unterminated_string_reports_its_opening_position():
    with pytest.raises(LexError, match="unterminated string") as info:
        tokenize("SELECT 'oops")
    assert info.value.start == 7
    assert info.value.column == 8


def test_a_backslash_does_not_escape_inside_a_string():
    # SQL escapes by doubling. Treating \' as an escape is a MySQL extension
    # whose behaviour depends on a server setting, and a classic injection bug.
    token = tokenize(r"'a\'")[0]
    assert token.value == "a\\"


# -- quoted identifiers ----------------------------------------------------


def test_a_quoted_identifier_can_be_a_reserved_word():
    token = tokenize('"select"')[0]
    assert token.type is TokenType.IDENTIFIER
    assert token.value == "select"


def test_a_quoted_identifier_preserves_case_and_spaces():
    token = tokenize('"My Column"')[0]
    assert token.value == "My Column"


def test_doubled_quotes_inside_a_quoted_identifier():
    assert tokenize('"a""b"')[0].value == 'a"b'


def test_an_empty_quoted_identifier_is_rejected():
    with pytest.raises(LexError, match="empty quoted identifier"):
        tokenize('""')


def test_an_unterminated_quoted_identifier_is_rejected():
    with pytest.raises(LexError, match="unterminated quoted identifier"):
        tokenize('"oops')


# -- operators -------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("=", TokenType.EQ),
        ("<>", TokenType.NEQ),
        ("!=", TokenType.NEQ),
        ("<", TokenType.LT),
        ("<=", TokenType.LTE),
        (">", TokenType.GT),
        (">=", TokenType.GTE),
        ("+", TokenType.PLUS),
        ("-", TokenType.MINUS),
        ("*", TokenType.STAR),
        ("/", TokenType.SLASH),
        ("%", TokenType.PERCENT),
    ],
)
def test_operators(sql: str, expected: TokenType):
    assert tokenize(sql)[0].type is expected


def test_two_character_operators_are_preferred_over_one():
    assert types("a<=b") == [
        TokenType.IDENTIFIER,
        TokenType.LTE,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]


def test_a_lone_bang_is_an_error():
    with pytest.raises(LexError, match=r"expected '=' after"):
        tokenize("a ! b")


def test_an_unknown_character_is_reported_with_its_position():
    with pytest.raises(LexError, match="unexpected character") as info:
        tokenize("SELECT # FROM t")
    assert info.value.column == 8


# -- comments and whitespace ----------------------------------------------


def test_line_comments_are_skipped():
    assert lexemes("SELECT -- everything\n* FROM t") == ["SELECT", "*", "FROM", "t"]


def test_a_line_comment_at_end_of_input_is_fine():
    assert lexemes("SELECT 1 -- done") == ["SELECT", "1"]


def test_block_comments_are_skipped_including_across_lines():
    assert lexemes("SELECT /* a\nb\nc */ 1") == ["SELECT", "1"]


def test_minus_minus_is_a_comment_but_minus_is_an_operator():
    assert types("1 - 2") == [
        TokenType.INT_LITERAL,
        TokenType.MINUS,
        TokenType.INT_LITERAL,
        TokenType.EOF,
    ]
    assert types("1 -- 2") == [TokenType.INT_LITERAL, TokenType.EOF]


def test_an_unterminated_block_comment_is_reported_at_its_start():
    with pytest.raises(LexError, match="unterminated block comment") as info:
        tokenize("SELECT /* oops")
    assert info.value.start == 7


def test_tabs_and_carriage_returns_are_whitespace_like_any_other():
    assert lexemes("SELECT\t*\r\nFROM\v t") == ["SELECT", "*", "FROM", "t"]


# -- realistic input -------------------------------------------------------


def test_a_full_create_table_tokenizes():
    sql = """
    -- users of the system
    CREATE TABLE users (
        id      INTEGER PRIMARY KEY,
        email   TEXT NOT NULL,
        age     INTEGER,
        active  BOOLEAN
    );
    """
    tokens = tokenize(sql)
    assert tokens[-1].type is TokenType.EOF
    keywords = [token.keyword for token in tokens if token.keyword]
    assert Keyword.CREATE in keywords
    assert Keyword.PRIMARY in keywords
    assert Keyword.INTEGER in keywords
    # Every span must still slice back to its lexeme in a multi-line input.
    for token in tokens[:-1]:
        assert token.span.text_in(sql) == token.lexeme


def test_tokenizing_is_linear_not_quadratic():
    # A regression guard: an accidental O(n^2) slice inside the scan loop would
    # blow this up long before the assertion.
    sql = "SELECT " + ", ".join(f"col{i}" for i in range(2000)) + " FROM t"
    tokens = tokenize(sql)
    assert len(tokens) == 1 + 2000 + 1999 + 2 + 1
