"""Parsing for editors: never raises, always returns something renderable.

:func:`parse` raises on the first problem, which is right for a caller that
wants to execute a statement.  An editor wants the opposite: show the tokens
that *did* scan, the statements that *did* parse, and put a marker on the part
that failed.

    "SELECT name FROM"
     ├── tokens:     SELECT · name · FROM · EOF      ← all four, scanned fine
     ├── statements: (none)
     └── error:      expected a table name, found end of input   at 16

Partial results are the point. A half-typed query is the normal state of a query
being written, and an editor that goes blank on every keystroke is useless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from engine.diagnostics.tracer import Tracer
from engine.errors import LexError, SqlError
from engine.parser.ast import Statement, walk
from engine.parser.lexer import Lexer
from engine.parser.parser import Parser
from engine.parser.tokens import Token

__all__ = ["ParseOutcome", "analyze_sql"]


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """Everything a parse produced, successful or not."""

    sql: str
    tokens: tuple[Token, ...]
    statements: tuple[Statement, ...]
    error: SqlError | None
    duration_ns: int
    lexed_ok: bool
    """False when tokenizing itself failed, so ``tokens`` is truncated."""

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def node_count(self) -> int:
        return sum(len(walk(statement)) for statement in self.statements)

    @property
    def statement_kinds(self) -> tuple[str, ...]:
        return tuple(statement.node_type for statement in self.statements)


def analyze_sql(sql: str, *, tracer: Tracer | None = None) -> ParseOutcome:
    """Tokenize and parse, capturing rather than raising any failure."""
    started = time.perf_counter_ns()

    tokens: tuple[Token, ...] = ()
    statements: tuple[Statement, ...] = ()
    error: SqlError | None = None
    lexed_ok = True

    lexer = Lexer(sql, tracer=tracer)
    try:
        tokens = tuple(lexer.tokenize())
    except LexError as exc:
        # The lexer raises at the offending character, so nothing after it is
        # trustworthy; report what the position was and stop.
        error = exc
        lexed_ok = False

    if lexed_ok:
        parser = Parser(list(tokens), sql, tracer=tracer)
        try:
            statements = tuple(parser.parse_script())
        except SqlError as exc:
            error = exc

    return ParseOutcome(
        sql=sql,
        tokens=tokens,
        statements=statements,
        error=error,
        duration_ns=time.perf_counter_ns() - started,
        lexed_ok=lexed_ok,
    )
