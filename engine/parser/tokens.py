"""Tokens, keywords, and source positions.

A :class:`Token` is a slice of the input plus a classification.  Crucially it
also carries a :class:`SourceSpan` — the exact character range it came from.
Every AST node inherits a span from the tokens it was built out of, which is
what lets the visualizer highlight the SQL text that produced any node.

Spans are half-open character offsets ``[start, end)`` into the original
string, plus a 1-based line and column for human-readable messages. Offsets
rather than line/column pairs are the primary representation because slicing
the source is then trivial, and because that is what a code editor's decoration
API wants.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = ["KEYWORDS", "TYPE_KEYWORDS", "Keyword", "SourceSpan", "Token", "TokenType"]


@dataclass(frozen=True, slots=True, order=True)
class SourceSpan:
    """A half-open character range ``[start, end)`` in the source text."""

    start: int
    end: int
    line: int = 1
    column: int = 1

    @property
    def length(self) -> int:
        return self.end - self.start

    def text_in(self, source: str) -> str:
        """The exact substring this span covers."""
        return source[self.start : self.end]

    def union(self, other: SourceSpan) -> SourceSpan:
        """Smallest span covering both.

        Used when a parse rule builds a node out of several tokens: the node's
        span runs from the first token's start to the last token's end, so
        selecting the node in the UI highlights the whole construct.
        """
        if self.start <= other.start:
            return SourceSpan(self.start, max(self.end, other.end), self.line, self.column)
        return SourceSpan(other.start, max(self.end, other.end), other.line, other.column)

    def __repr__(self) -> str:
        return f"[{self.start}:{self.end}]@{self.line}:{self.column}"


class TokenType(StrEnum):
    """What kind of thing a token is.

    Keywords all share :attr:`KEYWORD`, with the specific word in
    :attr:`Token.keyword`.  The alternative — one ``TokenType`` per keyword —
    makes the enum enormous and forces the lexer to know the full keyword set at
    the type level. Separating "it is a keyword" from "which keyword" keeps the
    parser's ``expect(TokenType.KEYWORD, Keyword.FROM)`` calls readable.
    """

    IDENTIFIER = "identifier"
    KEYWORD = "keyword"

    INT_LITERAL = "int_literal"
    FLOAT_LITERAL = "float_literal"
    STRING_LITERAL = "string_literal"

    STAR = "star"
    COMMA = "comma"
    SEMICOLON = "semicolon"
    LPAREN = "lparen"
    RPAREN = "rparen"
    DOT = "dot"

    EQ = "eq"
    NEQ = "neq"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    PLUS = "plus"
    MINUS = "minus"
    SLASH = "slash"
    PERCENT = "percent"

    EOF = "eof"

    @property
    def is_literal(self) -> bool:
        return self in _LITERAL_TYPES

    @property
    def is_operator(self) -> bool:
        return self in _OPERATOR_TYPES


_LITERAL_TYPES: Final = frozenset(
    {TokenType.INT_LITERAL, TokenType.FLOAT_LITERAL, TokenType.STRING_LITERAL}
)

_OPERATOR_TYPES: Final = frozenset(
    {
        TokenType.EQ,
        TokenType.NEQ,
        TokenType.LT,
        TokenType.LTE,
        TokenType.GT,
        TokenType.GTE,
        TokenType.PLUS,
        TokenType.MINUS,
        TokenType.STAR,
        TokenType.SLASH,
        TokenType.PERCENT,
    }
)


class Keyword(StrEnum):
    """Reserved words.

    The full set is reserved from the start, including words this milestone's
    parser does not accept yet.  Recognising ``ORDER`` as a keyword lets the
    parser say "ORDER BY is not implemented yet" instead of "unexpected
    identifier 'ORDER'", and it means a future milestone cannot silently break
    a query that used the word as a column name.
    """

    # Statements
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    CREATE = "CREATE"
    DROP = "DROP"
    EXPLAIN = "EXPLAIN"

    # Clauses
    FROM = "FROM"
    WHERE = "WHERE"
    INTO = "INTO"
    VALUES = "VALUES"
    SET = "SET"
    TABLE = "TABLE"
    INDEX = "INDEX"
    ON = "ON"
    AS = "AS"
    ORDER = "ORDER"
    GROUP = "GROUP"
    BY = "BY"
    LIMIT = "LIMIT"
    OFFSET = "OFFSET"
    ASC = "ASC"
    DESC = "DESC"
    DISTINCT = "DISTINCT"

    # Operators and predicates
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    IS = "IS"
    IN = "IN"
    LIKE = "LIKE"
    BETWEEN = "BETWEEN"

    # Literals
    NULL = "NULL"
    TRUE = "TRUE"
    FALSE = "FALSE"

    # Constraints
    PRIMARY = "PRIMARY"
    KEY = "KEY"
    UNIQUE = "UNIQUE"
    DEFAULT = "DEFAULT"
    IF = "IF"
    EXISTS = "EXISTS"

    # Type names
    INTEGER = "INTEGER"
    INT = "INT"
    BIGINT = "BIGINT"
    FLOAT = "FLOAT"
    REAL = "REAL"
    DOUBLE = "DOUBLE"
    BOOLEAN = "BOOLEAN"
    BOOL = "BOOL"
    TEXT = "TEXT"
    VARCHAR = "VARCHAR"

    # Transactions (Milestone 8)
    BEGIN = "BEGIN"
    COMMIT = "COMMIT"
    ROLLBACK = "ROLLBACK"


#: Lookup from an upper-cased word to its keyword. Identifiers are matched
#: case-insensitively, which is what every SQL dialect does.
KEYWORDS: Final[dict[str, Keyword]] = {keyword.value: keyword for keyword in Keyword}

#: Keywords that name a column type, so the parser can validate a column
#: definition without hard-coding the list in two places.
TYPE_KEYWORDS: Final[frozenset[Keyword]] = frozenset(
    {
        Keyword.INTEGER,
        Keyword.INT,
        Keyword.BIGINT,
        Keyword.FLOAT,
        Keyword.REAL,
        Keyword.DOUBLE,
        Keyword.BOOLEAN,
        Keyword.BOOL,
        Keyword.TEXT,
        Keyword.VARCHAR,
    }
)


@dataclass(frozen=True, slots=True)
class Token:
    """One lexical unit of the input."""

    type: TokenType
    lexeme: str
    """The exact source text, before any unescaping."""
    span: SourceSpan
    keyword: Keyword | None = None
    """Set when :attr:`type` is :attr:`TokenType.KEYWORD`."""
    value: Any = None
    """The decoded literal value: an ``int``, ``float`` or unescaped ``str``."""

    def is_keyword(self, *keywords: Keyword) -> bool:
        """Whether this token is one of ``keywords``."""
        return self.keyword is not None and self.keyword in keywords

    @property
    def description(self) -> str:
        """How the token should appear in an error message."""
        if self.type is TokenType.EOF:
            return "end of input"
        if self.type is TokenType.KEYWORD:
            return f"keyword {self.lexeme.upper()}"
        if self.type is TokenType.IDENTIFIER:
            return f"identifier {self.lexeme!r}"
        if self.type.is_literal:
            return f"literal {self.lexeme}"
        return f"{self.lexeme!r}"

    def __repr__(self) -> str:
        detail = self.keyword.value if self.keyword else self.lexeme
        return f"<{self.type.value} {detail!r} {self.span!r}>"
