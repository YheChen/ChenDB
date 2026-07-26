"""The SQL front end: text in, abstract syntax tree out.

    tokens.py   Token, TokenType, Keyword, SourceSpan
    lexer.py    hand-written scanner, one pass, one character of lookahead
    ast.py      frozen dataclass nodes, each carrying the source span it came from
    parser.py   recursive descent, one method per grammar rule

Milestone 2 parses ``CREATE TABLE``, ``INSERT`` and ``SELECT`` with a ``WHERE``
clause.  It does not *execute* anything — that is Milestone 3 — and it does not
check that a table or column exists, which needs the catalog in Milestone 4.
Parsing here is purely syntactic.

    from engine.parser import parse

    for statement in parse("SELECT name FROM users WHERE age >= 18"):
        print(statement.node_type, statement.span)
"""

from engine.parser.analyze import ParseOutcome, analyze_sql
from engine.parser.ast import (
    BinaryOp,
    BinaryOperator,
    ColumnConstraint,
    ColumnDefinition,
    ColumnRef,
    CreateTableStatement,
    Expression,
    InsertStatement,
    IsNullTest,
    Literal,
    Node,
    SelectItem,
    SelectStatement,
    Star,
    Statement,
    TableRef,
    UnaryOp,
    UnaryOperator,
    ValuesRow,
    walk,
)
from engine.parser.lexer import Lexer, tokenize
from engine.parser.parser import MAX_EXPRESSION_DEPTH, Parser, parse, parse_statement
from engine.parser.tokens import Keyword, SourceSpan, Token, TokenType

__all__ = [
    "MAX_EXPRESSION_DEPTH",
    "BinaryOp",
    "BinaryOperator",
    "ColumnConstraint",
    "ColumnDefinition",
    "ColumnRef",
    "CreateTableStatement",
    "Expression",
    "InsertStatement",
    "IsNullTest",
    "Keyword",
    "Lexer",
    "Literal",
    "Node",
    "ParseOutcome",
    "Parser",
    "SelectItem",
    "SelectStatement",
    "SourceSpan",
    "Star",
    "Statement",
    "TableRef",
    "Token",
    "TokenType",
    "UnaryOp",
    "UnaryOperator",
    "ValuesRow",
    "analyze_sql",
    "parse",
    "parse_statement",
    "tokenize",
    "walk",
]
