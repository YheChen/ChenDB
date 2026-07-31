"""Recursive-descent parser.

One method per grammar rule.  The call stack *is* the parse tree, which is the
whole appeal: the grammar below can be read straight off the method names.

    script         := statement { ';' statement } [ ';' ]
    statement      := create_table | create_index | insert | select
                    | update | delete
                    | explain | analyze | begin | commit | rollback

    begin          := BEGIN [ TRANSACTION ]
    commit         := COMMIT [ TRANSACTION ]
    rollback       := ROLLBACK [ TRANSACTION ]

    explain        := EXPLAIN [ ANALYZE ] statement
    analyze        := ANALYZE [ ident ]

    create_table   := CREATE TABLE [ IF NOT EXISTS ] ident
                      '(' column_def { ',' column_def } ')'
    column_def     := ident type_name { constraint }
    constraint     := NOT NULL | NULL | PRIMARY KEY | UNIQUE

    create_index   := CREATE [ UNIQUE ] INDEX [ IF NOT EXISTS ] ident
                      ON ident '(' ident ')'

    insert         := INSERT INTO ident [ '(' ident { ',' ident } ')' ]
                      VALUES row { ',' row }
    row            := '(' expr { ',' expr } ')'

    select         := SELECT select_list FROM from_clause [ WHERE expr ]
                      [ GROUP BY expr { ',' expr } ] [ HAVING expr ]
                      [ ORDER BY sort_item { ',' sort_item } ]
                      [ LIMIT int [ OFFSET int ] ]
    select_list    := select_item { ',' select_item }
    select_item    := '*' | ident '.' '*' | expr [ [ AS ] ident ]

    from_clause    := table_ref { ',' table_ref | join }
    table_ref      := ident [ [ AS ] ident ]
    join           := [ INNER | ( LEFT | RIGHT | FULL ) [ OUTER ] ]
                      JOIN table_ref ON expr
    sort_item      := expr [ ASC | DESC ]

    update         := UPDATE ident SET assignment { ',' assignment }
                      [ WHERE expr ]
    assignment     := ident '=' expr

    delete         := DELETE FROM ident [ WHERE expr ]

    expr           := or_expr
    or_expr        := and_expr { OR and_expr }
    and_expr       := not_expr { AND not_expr }
    not_expr       := NOT not_expr | comparison
    comparison     := additive [ ( '=' | '<>' | '<' | '<=' | '>' | '>=' ) additive
                               | IS [ NOT ] NULL ]
    additive       := multiplicative { ( '+' | '-' ) multiplicative }
    multiplicative := unary { ( '*' | '/' | '%' ) unary }
    unary          := ( '-' | '+' ) unary | primary
    primary        := literal | column_ref | aggregate | '(' expr ')'
    column_ref     := ident [ '.' ident ]
    aggregate      := ( COUNT | SUM | AVG | MIN | MAX ) '(' ( '*' | expr ) ')'

Precedence is encoded in the *shape* of the rules: ``or_expr`` calls
``and_expr`` calls ``not_expr`` and so on, so the operator parsed at the
shallowest level binds loosest.  ``a OR b AND c`` therefore parses as
``a OR (b AND c)`` without any precedence table.  Left associativity comes from
the ``while`` loops: ``1 - 2 - 3`` becomes ``(1 - 2) - 3``.

Why recursive descent
---------------------
It is the technique used by essentially every hand-written production parser,
including SQLite, Clang, TypeScript and Go, because errors can be reported at
the exact point of failure with the rule name for context. Table-driven
LALR parsers (PostgreSQL's ``gram.y``, via Bison) handle larger grammars and
resolve ambiguity mechanically, but produce famously unhelpful messages,
"syntax error at or near" is the limit of what a shift-reduce conflict can tell
you.

Complexity: O(n) in tokens. Each token is consumed once; there is no
backtracking, only one token of lookahead. Depth is bounded by expression
nesting, so a pathological ``((((...))))`` could exhaust the Python stack,
:data:`MAX_EXPRESSION_DEPTH` turns that into a clean error instead of a crash.
"""

from __future__ import annotations

import time
from typing import Final, NoReturn

from engine.diagnostics.events import AstNodeCreatedEvent, ParsedEvent, ParseErrorEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import ParseError, UnsupportedSqlError
from engine.parser.ast import (
    AggregateFunction,
    AnalyzeStatement,
    Assignment,
    BeginStatement,
    BinaryOp,
    BinaryOperator,
    ColumnConstraint,
    ColumnDefinition,
    ColumnRef,
    CommitStatement,
    CreateIndexStatement,
    CreateTableStatement,
    DeleteStatement,
    ExplainStatement,
    Expression,
    FunctionCall,
    InsertStatement,
    IsNullTest,
    JoinClause,
    JoinKind,
    Literal,
    Node,
    OrderByItem,
    RollbackStatement,
    ScalarSubquery,
    SelectItem,
    SelectStatement,
    SortDirection,
    Star,
    Statement,
    TableRef,
    UnaryOp,
    UnaryOperator,
    UpdateStatement,
    ValuesRow,
)
from engine.parser.lexer import tokenize
from engine.parser.tokens import (
    TYPE_KEYWORDS,
    Keyword,
    SourceSpan,
    Token,
    TokenType,
)
from engine.serialization.types import DataType

__all__ = ["MAX_EXPRESSION_DEPTH", "Parser", "parse", "parse_statement"]

#: Guard against stack exhaustion from deeply nested parentheses. CPython's
#: default recursion limit would otherwise turn a hostile query into a crash.
MAX_EXPRESSION_DEPTH: Final = 100

#: Comparison tokens mapped to their AST operator.
_COMPARISON_OPERATORS: Final[dict[TokenType, BinaryOperator]] = {
    TokenType.EQ: BinaryOperator.EQ,
    TokenType.NEQ: BinaryOperator.NEQ,
    TokenType.LT: BinaryOperator.LT,
    TokenType.LTE: BinaryOperator.LTE,
    TokenType.GT: BinaryOperator.GT,
    TokenType.GTE: BinaryOperator.GTE,
}

_ADDITIVE_OPERATORS: Final[dict[TokenType, BinaryOperator]] = {
    TokenType.PLUS: BinaryOperator.ADD,
    TokenType.MINUS: BinaryOperator.SUBTRACT,
}

_MULTIPLICATIVE_OPERATORS: Final[dict[TokenType, BinaryOperator]] = {
    TokenType.STAR: BinaryOperator.MULTIPLY,
    TokenType.SLASH: BinaryOperator.DIVIDE,
    TokenType.PERCENT: BinaryOperator.MODULO,
}

#: SQL type keywords mapped to the engine's types. Several spellings share one
#: physical type, exactly as in SQLite.
_TYPE_BY_KEYWORD: Final[dict[Keyword, DataType]] = {
    Keyword.INTEGER: DataType.INTEGER,
    Keyword.INT: DataType.INTEGER,
    Keyword.BIGINT: DataType.INTEGER,
    Keyword.FLOAT: DataType.FLOAT,
    Keyword.REAL: DataType.FLOAT,
    Keyword.DOUBLE: DataType.FLOAT,
    Keyword.BOOLEAN: DataType.BOOLEAN,
    Keyword.BOOL: DataType.BOOLEAN,
    Keyword.TEXT: DataType.TEXT,
    Keyword.VARCHAR: DataType.TEXT,
}

#: Statement keywords that are valid SQL but not implemented yet, with the
#: milestone that will add them. Recognising them lets the parser explain
#: itself instead of reporting a generic syntax error.
_NOT_YET: Final[dict[Keyword, str]] = {
    Keyword.DROP: "DROP is not implemented yet",
}


class Parser:
    """Turns a token stream into statements. Single use."""

    __slots__ = ("_depth", "_next_node_id", "_pos", "_source", "_tokens", "_tracer")

    def __init__(
        self,
        tokens: list[Token],
        source: str = "",
        *,
        tracer: Tracer | None = None,
    ) -> None:
        self._tokens = tokens
        self._source = source
        self._pos = 0
        self._next_node_id = 0
        self._depth = 0
        self._tracer = tracer if tracer is not None else NULL_TRACER

    # -- token access ------------------------------------------------------

    @property
    def _current(self) -> Token:
        return self._tokens[self._pos]

    def _at_end(self) -> bool:
        return self._current.type is TokenType.EOF

    def _check(self, token_type: TokenType) -> bool:
        return self._current.type is token_type

    def _check_keyword(self, *keywords: Keyword) -> bool:
        return self._current.is_keyword(*keywords)

    def _advance(self) -> Token:
        token = self._current
        if not self._at_end():
            self._pos += 1
        return token

    def _match(self, token_type: TokenType) -> Token | None:
        """Consume and return the token if it matches, else return ``None``."""
        return self._advance() if self._check(token_type) else None

    def _match_keyword(self, *keywords: Keyword) -> Token | None:
        return self._advance() if self._check_keyword(*keywords) else None

    def _expect(self, token_type: TokenType, description: str) -> Token:
        if not self._check(token_type):
            self._fail(f"expected {description}", expected=(description,))
        return self._advance()

    def _expect_keyword(self, keyword: Keyword) -> Token:
        if not self._check_keyword(keyword):
            self._fail(f"expected {keyword.value}", expected=(keyword.value,))
        return self._advance()

    def _fail(self, message: str, *, expected: tuple[str, ...] = ()) -> NoReturn:
        token = self._current
        full = f"{message}, found {token.description}"
        if self._tracer.operator:
            self._tracer.emit(
                ParseErrorEvent(
                    message=full,
                    start=token.span.start,
                    end=token.span.end,
                    line=token.span.line,
                    column=token.span.column,
                    expected=", ".join(expected),
                    found=token.description,
                )
            )
        raise ParseError(
            full,
            start=token.span.start,
            end=token.span.end,
            line=token.span.line,
            column=token.span.column,
            expected=expected,
            found=token.description,
        )

    def _unsupported(self, message: str) -> NoReturn:
        token = self._current
        raise UnsupportedSqlError(
            message,
            start=token.span.start,
            end=token.span.end,
            line=token.span.line,
            column=token.span.column,
            found=token.description,
        )

    # -- node construction -------------------------------------------------

    def _node[T: Node](self, factory: type[T], span: SourceSpan, **fields: object) -> T:
        """Build a node, assign its id, and report it."""
        node_id = self._next_node_id
        self._next_node_id += 1
        node = factory(node_id=node_id, span=span, **fields)  # type: ignore[arg-type]
        if self._tracer.verbose:
            self._tracer.emit(
                AstNodeCreatedEvent(
                    node_id=node_id,
                    node_type=node.node_type,
                    start=span.start,
                    end=span.end,
                    child_count=len(node.children()),
                )
            )
        return node

    def _identifier(self, what: str) -> Token:
        """Consume an identifier, with a clear error when a keyword appears.

        Reserved words are the most common source of confusing parse errors, so
        the message names the fix rather than just the problem.
        """
        if self._check(TokenType.KEYWORD):
            keyword = self._current.keyword
            self._fail(
                f"expected {what}, but {keyword.value if keyword else '?'} is a "
                f'reserved word; quote it as "{self._current.lexeme}" to use it '
                f"as a name",
                expected=(what,),
            )
        token = self._expect(TokenType.IDENTIFIER, what)
        return token

    @staticmethod
    def _identifier_name(token: Token) -> str:
        """The name a token denotes. Quoted identifiers keep their exact case."""
        return token.value if token.value is not None else token.lexeme

    # -- entry points ------------------------------------------------------

    def parse_script(self) -> list[Statement]:
        """Parse every statement in the input.

        A trailing semicolon is optional, and consecutive semicolons are
        tolerated, both are what people actually type.
        """
        started = time.perf_counter_ns()
        statements: list[Statement] = []

        while not self._at_end():
            if self._match(TokenType.SEMICOLON):
                continue
            statements.append(self.parse_statement())
            if not self._at_end() and not self._check(TokenType.SEMICOLON):
                self._fail(
                    "expected ';' between statements", expected=(";", "end of input")
                )

        if self._tracer.operator:
            self._tracer.emit(
                ParsedEvent(
                    statement_count=len(statements),
                    node_count=self._next_node_id,
                    duration_ns=time.perf_counter_ns() - started,
                )
            )
        return statements

    def parse_statement(self) -> Statement:
        """Dispatch on the leading keyword."""
        if self._check_keyword(Keyword.SELECT):
            return self._select_statement()
        if self._check_keyword(Keyword.INSERT):
            return self._insert_statement()
        if self._check_keyword(Keyword.UPDATE):
            return self._update_statement()
        if self._check_keyword(Keyword.DELETE):
            return self._delete_statement()
        if self._check_keyword(Keyword.CREATE):
            return self._create_statement()
        if self._check_keyword(Keyword.EXPLAIN):
            return self._explain_statement()
        if self._check_keyword(Keyword.ANALYZE):
            return self._analyze_statement()
        if self._check_keyword(Keyword.BEGIN, Keyword.COMMIT, Keyword.ROLLBACK):
            return self._transaction_statement()

        keyword = self._current.keyword
        if keyword is not None and keyword in _NOT_YET:
            self._unsupported(_NOT_YET[keyword])
        self._fail(
            "expected a statement",
            expected=(
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "CREATE TABLE",
                "CREATE INDEX",
                "EXPLAIN",
                "ANALYZE",
                "BEGIN",
                "COMMIT",
                "ROLLBACK",
            ),
        )

    # -- transactions ------------------------------------------------------

    def _transaction_statement(self) -> Statement:
        """``BEGIN`` / ``COMMIT`` / ``ROLLBACK``, with an optional TRANSACTION.

        The noise word is accepted and discarded, as in every SQL dialect: it
        reads better in a script and means nothing.
        """
        token = self._advance()
        span = token.span
        noise = self._match_keyword(Keyword.TRANSACTION)
        if noise is not None:
            span = span.union(noise.span)
        node = {
            Keyword.BEGIN: BeginStatement,
            Keyword.COMMIT: CommitStatement,
            Keyword.ROLLBACK: RollbackStatement,
        }[token.keyword]
        return self._node(node, span)

    # -- EXPLAIN / ANALYZE -------------------------------------------------

    def _explain_statement(self) -> ExplainStatement:
        start = self._expect_keyword(Keyword.EXPLAIN).span
        analyze = self._match_keyword(Keyword.ANALYZE) is not None
        inner = self.parse_statement()
        if isinstance(inner, ExplainStatement):
            self._unsupported("EXPLAIN cannot explain another EXPLAIN")
        return self._node(
            ExplainStatement,
            start.union(inner.span),
            statement=inner,
            analyze=analyze,
        )

    def _analyze_statement(self) -> AnalyzeStatement:
        start = self._expect_keyword(Keyword.ANALYZE).span
        table = None
        span = start
        if self._check(TokenType.IDENTIFIER) or (
            self._check(TokenType.KEYWORD) and not self._check(TokenType.SEMICOLON)
        ):
            table = self._table_ref()
            span = start.union(table.span)
        return self._node(AnalyzeStatement, span, table=table)

    # -- CREATE ------------------------------------------------------------

    def _create_statement(self) -> Statement:
        """Dispatch on what follows ``CREATE``.

        Two tokens of lookahead would be tidier, but the parser keeps to one, so
        ``CREATE`` is consumed here and both branches continue from there.
        """
        start = self._expect_keyword(Keyword.CREATE).span
        if self._check_keyword(Keyword.UNIQUE, Keyword.INDEX):
            return self._create_index_statement(start)
        return self._create_table_statement(start)

    def _create_index_statement(self, start: SourceSpan) -> CreateIndexStatement:
        unique = self._match_keyword(Keyword.UNIQUE) is not None
        self._expect_keyword(Keyword.INDEX)

        if_not_exists = False
        if self._match_keyword(Keyword.IF):
            self._expect_keyword(Keyword.NOT)
            self._expect_keyword(Keyword.EXISTS)
            if_not_exists = True

        index_name = self._identifier_name(self._identifier("an index name"))
        self._expect_keyword(Keyword.ON)
        table = self._table_ref()
        self._expect(TokenType.LPAREN, "'(' before the indexed column")
        column = self._identifier_name(self._identifier("a column name"))
        if self._check(TokenType.COMMA):
            self._unsupported(
                "a multi-column index needs a composite key encoding, which "
                "ChenDB does not implement"
            )
        end = self._expect(TokenType.RPAREN, "')' after the indexed column").span

        return self._node(
            CreateIndexStatement,
            start.union(end),
            index_name=index_name,
            table=table,
            column=column,
            unique=unique,
            if_not_exists=if_not_exists,
        )

    # -- CREATE TABLE ------------------------------------------------------

    def _create_table_statement(self, start: SourceSpan) -> CreateTableStatement:
        self._expect_keyword(Keyword.TABLE)

        if_not_exists = False
        if self._match_keyword(Keyword.IF):
            self._expect_keyword(Keyword.NOT)
            self._expect_keyword(Keyword.EXISTS)
            if_not_exists = True

        table = self._table_ref()
        self._expect(TokenType.LPAREN, "'(' before the column list")

        columns: list[ColumnDefinition] = []
        while True:
            columns.append(self._column_definition())
            if not self._match(TokenType.COMMA):
                break
        end = self._expect(TokenType.RPAREN, "')' after the column list").span

        return self._node(
            CreateTableStatement,
            start.union(end),
            table=table,
            columns=tuple(columns),
            if_not_exists=if_not_exists,
        )

    def _column_definition(self) -> ColumnDefinition:
        name_token = self._identifier("a column name")
        span = name_token.span

        if not self._check(TokenType.KEYWORD) or self._current.keyword not in TYPE_KEYWORDS:
            self._fail(
                "expected a column type",
                expected=tuple(sorted({t.value for t in TYPE_KEYWORDS})),
            )
        type_token = self._advance()
        span = span.union(type_token.span)
        data_type = _TYPE_BY_KEYWORD[type_token.keyword]  # type: ignore[index]

        # VARCHAR(n) parses but the length is ignored: TEXT is variable-width
        # already, so a declared maximum would be a constraint, not a layout.
        if type_token.is_keyword(Keyword.VARCHAR) and self._match(TokenType.LPAREN):
            self._expect(TokenType.INT_LITERAL, "a length")
            span = span.union(self._expect(TokenType.RPAREN, "')'").span)

        constraints: list[ColumnConstraint] = []
        while True:
            constraint, constraint_span = self._column_constraint()
            if constraint is None:
                break
            if constraint in constraints:
                self._fail(f"duplicate constraint {constraint.value}")
            constraints.append(constraint)
            span = span.union(constraint_span)

        if (
            ColumnConstraint.NULL in constraints
            and ColumnConstraint.NOT_NULL in constraints
        ):
            self._fail("a column cannot be both NULL and NOT NULL")

        return self._node(
            ColumnDefinition,
            span,
            name=self._identifier_name(name_token),
            data_type=data_type,
            constraints=tuple(constraints),
        )

    def _column_constraint(self) -> tuple[ColumnConstraint | None, SourceSpan]:
        token = self._current
        if self._match_keyword(Keyword.NOT):
            end = self._expect_keyword(Keyword.NULL).span
            return ColumnConstraint.NOT_NULL, token.span.union(end)
        if self._match_keyword(Keyword.NULL):
            return ColumnConstraint.NULL, token.span
        if self._match_keyword(Keyword.PRIMARY):
            end = self._expect_keyword(Keyword.KEY).span
            return ColumnConstraint.PRIMARY_KEY, token.span.union(end)
        if self._check_keyword(Keyword.UNIQUE):
            self._unsupported(
                "an inline UNIQUE constraint is not implemented; "
                "use CREATE UNIQUE INDEX instead"
            )
        if self._check_keyword(Keyword.DEFAULT):
            self._unsupported("DEFAULT values are not implemented yet")
        return None, token.span

    # -- INSERT ------------------------------------------------------------

    def _insert_statement(self) -> InsertStatement:
        start = self._expect_keyword(Keyword.INSERT).span
        self._expect_keyword(Keyword.INTO)
        table = self._table_ref()

        columns: tuple[str, ...] | None = None
        if self._match(TokenType.LPAREN):
            names: list[str] = []
            while True:
                names.append(self._identifier_name(self._identifier("a column name")))
                if not self._match(TokenType.COMMA):
                    break
            self._expect(TokenType.RPAREN, "')' after the column list")
            columns = tuple(names)

        if self._check_keyword(Keyword.SELECT):
            self._unsupported("INSERT ... SELECT needs the executor, in Milestone 3")
        self._expect_keyword(Keyword.VALUES)

        rows: list[ValuesRow] = []
        end = start
        while True:
            open_paren = self._expect(TokenType.LPAREN, "'(' before a row of values")
            values: list[Expression] = []
            while True:
                values.append(self._expression())
                if not self._match(TokenType.COMMA):
                    break
            end = self._expect(TokenType.RPAREN, "')' after a row of values").span
            rows.append(
                self._node(ValuesRow, open_paren.span.union(end), values=tuple(values))
            )
            if not self._match(TokenType.COMMA):
                break

        # Caught here rather than at execution: it is a shape error in the
        # statement itself, and the message can point at the source.
        if columns is not None:
            for row in rows:
                if row.width != len(columns):
                    self._fail(
                        f"row has {row.width} values but {len(columns)} columns were named"
                    )
        widths = {row.width for row in rows}
        if len(widths) > 1:
            self._fail(
                f"every row must have the same number of values; found {sorted(widths)}"
            )

        return self._node(
            InsertStatement,
            start.union(end),
            table=table,
            columns=columns,
            rows=tuple(rows),
        )

    # -- UPDATE / DELETE ---------------------------------------------------

    def _update_statement(self) -> UpdateStatement:
        start = self._expect_keyword(Keyword.UPDATE).span
        table = self._table_ref()
        self._expect_keyword(Keyword.SET)

        assignments: list[Assignment] = []
        while True:
            assignments.append(self._assignment())
            if not self._match(TokenType.COMMA):
                break
        end = assignments[-1].span

        if self._check_keyword(Keyword.FROM):
            self._unsupported(
                "UPDATE ... FROM needs a second row source, and there are no joins yet"
            )

        where: Expression | None = None
        if self._match_keyword(Keyword.WHERE):
            where = self._expression()
            end = where.span

        self._reject_trailing_clauses("UPDATE")
        return self._node(
            UpdateStatement,
            start.union(end),
            table=table,
            assignments=tuple(assignments),
            where=where,
        )

    def _assignment(self) -> Assignment:
        name_token = self._identifier("a column name")
        self._expect(TokenType.EQ, "'=' after the column name")
        value = self._expression()
        return self._node(
            Assignment,
            name_token.span.union(value.span),
            column=self._identifier_name(name_token),
            value=value,
        )

    def _delete_statement(self) -> DeleteStatement:
        start = self._expect_keyword(Keyword.DELETE).span
        # FROM is optional in MySQL's `DELETE t WHERE ...`; requiring it keeps
        # the one-token lookahead honest and matches the standard.
        self._expect_keyword(Keyword.FROM)
        table = self._table_ref()
        end = table.span

        where: Expression | None = None
        if self._match_keyword(Keyword.WHERE):
            where = self._expression()
            end = where.span

        self._reject_trailing_clauses("DELETE")
        return self._node(DeleteStatement, start.union(end), table=table, where=where)

    def _reject_trailing_clauses(self, what: str) -> None:
        """Reject ``ORDER BY``/``LIMIT`` on a statement that cannot honour them.

        MySQL accepts both on ``UPDATE`` and ``DELETE``; PostgreSQL accepts
        neither, because without an ordering guarantee "delete 10 rows" does not
        say *which* ten. Failing loudly beats silently ignoring the clause,
        which is the failure mode that loses data.
        """
        for keyword, clause in ((Keyword.ORDER, "ORDER BY"), (Keyword.LIMIT, "LIMIT")):
            if self._check_keyword(keyword):
                self._unsupported(f"{clause} is not allowed on {what}")

    # -- SELECT ------------------------------------------------------------

    def _select_statement(self) -> SelectStatement:
        start = self._expect_keyword(Keyword.SELECT).span
        if self._check_keyword(Keyword.DISTINCT):
            self._unsupported("SELECT DISTINCT is not implemented yet")

        projections: list[SelectItem] = []
        while True:
            projections.append(self._select_item())
            if not self._match(TokenType.COMMA):
                break

        self._expect_keyword(Keyword.FROM)
        table, joins = self._from_clause()
        end = joins[-1].span if joins else table.span

        where: Expression | None = None
        if self._match_keyword(Keyword.WHERE):
            where = self._expression()
            end = where.span

        group_by: tuple[Expression, ...] = ()
        if self._match_keyword(Keyword.GROUP):
            self._expect_keyword(Keyword.BY)
            keys: list[Expression] = []
            while True:
                keys.append(self._expression())
                if not self._match(TokenType.COMMA):
                    break
            group_by = tuple(keys)
            end = group_by[-1].span

        having: Expression | None = None
        if self._match_keyword(Keyword.HAVING):
            having = self._expression()
            end = having.span

        order_by: tuple[OrderByItem, ...] = ()
        if self._match_keyword(Keyword.ORDER):
            self._expect_keyword(Keyword.BY)
            items: list[OrderByItem] = []
            while True:
                items.append(self._sort_item())
                if not self._match(TokenType.COMMA):
                    break
            order_by = tuple(items)
            end = order_by[-1].span

        limit: int | None = None
        offset: int | None = None
        if self._match_keyword(Keyword.LIMIT):
            limit_token = self._expect(TokenType.INT_LITERAL, "a row count after LIMIT")
            limit = int(limit_token.value)  # type: ignore[arg-type]
            end = limit_token.span
            if limit < 0:  # pragma: no cover - the lexer has no negative literals
                self._fail("LIMIT cannot be negative")
        if self._match_keyword(Keyword.OFFSET):
            if limit is None:
                # Accepted by PostgreSQL, and it means "skip n, then all the
                # rest". Refused here because the executor implements OFFSET as
                # part of LIMIT and would silently ignore a lone one.
                self._unsupported("OFFSET without LIMIT is not implemented")
            offset_token = self._expect(TokenType.INT_LITERAL, "a row count after OFFSET")
            offset = int(offset_token.value)  # type: ignore[arg-type]
            end = offset_token.span

        return self._node(
            SelectStatement,
            start.union(end),
            projections=tuple(projections),
            table=table,
            joins=joins,
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

    def _from_clause(self) -> tuple[TableRef, tuple[JoinClause, ...]]:
        """``a``, ``a, b``, or ``a JOIN b ON …``, the same thing three ways.

        A comma-separated ``FROM`` is an inner join with its predicate in the
        ``WHERE`` clause, and the planner treats it identically. Supporting both
        spellings costs one branch and is worth it: the comma form is how most
        older SQL is written, and rejecting it would make the join planner
        unreachable from half the queries anyone would try.
        """
        first = self._table_ref()
        joins: list[JoinClause] = []

        while True:
            if self._match(TokenType.COMMA):
                table = self._table_ref()
                # No ON. The predicate, if any, is in the WHERE clause, and
                # `TRUE` is the honest join condition for a cross product.
                joins.append(
                    self._node(
                        JoinClause,
                        table.span,
                        table=table,
                        on=self._node(
                            Literal, table.span, value=True, data_type=DataType.BOOLEAN
                        ),
                        kind=JoinKind.INNER,
                    )
                )
                continue

            kind = self._join_kind()
            if kind is None:
                break
            start = self._expect_keyword(Keyword.JOIN).span
            table = self._table_ref()
            self._expect_keyword(Keyword.ON)
            on = self._expression()
            joins.append(
                self._node(JoinClause, start.union(on.span), table=table, on=on, kind=kind)
            )

        return first, tuple(joins)

    def _join_kind(self) -> JoinKind | None:
        """The join flavour about to be parsed, or ``None`` if this is not one.

        ``OUTER`` is noise everywhere it is allowed (``LEFT JOIN`` and ``LEFT
        OUTER JOIN`` are the same thing in the standard) so it is accepted and
        discarded rather than recorded. What is *not* noise is which side the
        keyword names: that is the whole difference between an inner join and an
        outer one, and everything from here to the executor carries it.
        """
        if self._match_keyword(Keyword.INNER):
            return JoinKind.INNER
        if (
            token := self._match_keyword(Keyword.LEFT, Keyword.RIGHT, Keyword.FULL)
        ) is not None:
            self._match_keyword(Keyword.OUTER)
            side = token.keyword
            assert side is not None
            return JoinKind(side.value)
        if self._check_keyword(Keyword.CROSS):
            self._unsupported("CROSS JOIN is not implemented; write 'FROM a, b'")
        if self._check_keyword(Keyword.JOIN):
            return JoinKind.INNER
        return None

    def _sort_item(self) -> OrderByItem:
        expression = self._expression()
        span = expression.span
        direction = SortDirection.ASC
        if (token := self._match_keyword(Keyword.ASC, Keyword.DESC)) is not None:
            direction = (
                SortDirection.DESC if token.is_keyword(Keyword.DESC) else SortDirection.ASC
            )
            span = span.union(token.span)
        return self._node(OrderByItem, span, expression=expression, direction=direction)

    def _select_item(self) -> SelectItem:
        if self._check(TokenType.STAR):
            star_token = self._advance()
            star = self._node(Star, star_token.span, table=None)
            return self._node(SelectItem, star_token.span, expression=star, alias=None)

        # `u.*`: every column of one table in a join. One token of lookahead is
        # not enough to see it coming, so the qualified reference is parsed and
        # the star recognised after the dot.
        if self._check(TokenType.IDENTIFIER) and self._peek_is_qualified_star():
            name_token = self._advance()
            self._advance()  # the dot
            star_token = self._advance()  # the star
            star = self._node(
                Star,
                name_token.span.union(star_token.span),
                table=self._identifier_name(name_token),
            )
            return self._node(SelectItem, star.span, expression=star, alias=None)

        expression = self._expression()
        span = expression.span
        alias: str | None = None

        if self._match_keyword(Keyword.AS):
            alias_token = self._identifier("an alias")
            alias = self._identifier_name(alias_token)
            span = span.union(alias_token.span)
        elif self._check(TokenType.IDENTIFIER):
            # `SELECT age years`: the alias without AS. Accepted because
            # every dialect does, though AS is clearer.
            alias_token = self._advance()
            alias = self._identifier_name(alias_token)
            span = span.union(alias_token.span)

        return self._node(SelectItem, span, expression=expression, alias=alias)

    def _peek_is_qualified_star(self) -> bool:
        """Two tokens ahead: is this ``ident . *``?

        The only place the parser looks past one token, and it is bounded and
        local. The alternative is a backtracking attempt at an expression,
        which would cost the "no backtracking" property the whole design rests
        on for a syntax used in one position.
        """
        return (
            self._pos + 2 < len(self._tokens)
            and self._tokens[self._pos + 1].type is TokenType.DOT
            and self._tokens[self._pos + 2].type is TokenType.STAR
        )

    def _table_ref(self) -> TableRef:
        token = self._identifier("a table name")
        span = token.span
        alias: str | None = None

        if self._match_keyword(Keyword.AS):
            alias_token = self._identifier("a table alias")
            alias = self._identifier_name(alias_token)
            span = span.union(alias_token.span)
        elif self._check(TokenType.IDENTIFIER):
            # `FROM users u`. Only an identifier can follow a table name here (
            # every clause that could come next starts with a keyword) so this
            # needs no lookahead.
            alias_token = self._advance()
            alias = self._identifier_name(alias_token)
            span = span.union(alias_token.span)

        return self._node(TableRef, span, name=self._identifier_name(token), alias=alias)

    # -- expressions -------------------------------------------------------

    def _expression(self) -> Expression:
        self._depth += 1
        if self._depth > MAX_EXPRESSION_DEPTH:
            self._depth -= 1
            self._fail(f"expression nested deeper than {MAX_EXPRESSION_DEPTH} levels")
        try:
            return self._or_expression()
        finally:
            self._depth -= 1

    def _or_expression(self) -> Expression:
        left = self._and_expression()
        while self._check_keyword(Keyword.OR):
            self._advance()
            right = self._and_expression()
            left = self._node(
                BinaryOp,
                left.span.union(right.span),
                operator=BinaryOperator.OR,
                left=left,
                right=right,
            )
        return left

    def _and_expression(self) -> Expression:
        left = self._not_expression()
        while self._check_keyword(Keyword.AND):
            self._advance()
            right = self._not_expression()
            left = self._node(
                BinaryOp,
                left.span.union(right.span),
                operator=BinaryOperator.AND,
                left=left,
                right=right,
            )
        return left

    def _not_expression(self) -> Expression:
        if self._check_keyword(Keyword.NOT):
            token = self._advance()
            operand = self._not_expression()
            return self._node(
                UnaryOp,
                token.span.union(operand.span),
                operator=UnaryOperator.NOT,
                operand=operand,
            )
        return self._comparison()

    def _comparison(self) -> Expression:
        left = self._additive()

        operator = _COMPARISON_OPERATORS.get(self._current.type)
        if operator is not None:
            self._advance()
            right = self._additive()
            return self._node(
                BinaryOp,
                left.span.union(right.span),
                operator=operator,
                left=left,
                right=right,
            )

        if self._check_keyword(Keyword.IS):
            self._advance()
            negated = self._match_keyword(Keyword.NOT) is not None
            null_token = self._expect_keyword(Keyword.NULL)
            return self._node(
                IsNullTest,
                left.span.union(null_token.span),
                operand=left,
                negated=negated,
            )

        for keyword, message in (
            (Keyword.IN, "IN is not implemented yet"),
            (Keyword.LIKE, "LIKE is not implemented yet"),
            (Keyword.BETWEEN, "BETWEEN is not implemented yet"),
        ):
            if self._check_keyword(keyword):
                self._unsupported(message)

        return left

    def _additive(self) -> Expression:
        left = self._multiplicative()
        while (operator := _ADDITIVE_OPERATORS.get(self._current.type)) is not None:
            self._advance()
            right = self._multiplicative()
            left = self._node(
                BinaryOp,
                left.span.union(right.span),
                operator=operator,
                left=left,
                right=right,
            )
        return left

    def _multiplicative(self) -> Expression:
        left = self._unary()
        while (operator := _MULTIPLICATIVE_OPERATORS.get(self._current.type)) is not None:
            self._advance()
            right = self._unary()
            left = self._node(
                BinaryOp,
                left.span.union(right.span),
                operator=operator,
                left=left,
                right=right,
            )
        return left

    def _unary(self) -> Expression:
        if self._check(TokenType.MINUS) or self._check(TokenType.PLUS):
            token = self._advance()
            operand = self._unary()
            operator = (
                UnaryOperator.NEGATE
                if token.type is TokenType.MINUS
                else UnaryOperator.PLUS
            )
            return self._node(
                UnaryOp,
                token.span.union(operand.span),
                operator=operator,
                operand=operand,
            )
        return self._primary()

    def _primary(self) -> Expression:
        token = self._current

        if self._match(TokenType.LPAREN):
            if self._check_keyword(Keyword.SELECT):
                # `(SELECT …)` where a value is expected. The only place this
                # grammar recurses into a whole statement, and the reason
                # `_select_statement` had to stop assuming it owned the tokens
                # to the end of the input.
                inner_select = self._select_statement()
                end = self._expect(TokenType.RPAREN, "')' after a subquery").span
                return self._node(
                    ScalarSubquery, token.span.union(end), statement=inner_select
                )
            inner = self._expression()
            end = self._expect(TokenType.RPAREN, "')'").span
            # The parenthesised node's span covers the brackets, so selecting it
            # in the UI highlights `(a + b)` rather than `a + b`.
            return _with_span(inner, token.span.union(end))

        if self._check(TokenType.INT_LITERAL):
            self._advance()
            return self._node(
                Literal, token.span, value=token.value, data_type=DataType.INTEGER
            )
        if self._check(TokenType.FLOAT_LITERAL):
            self._advance()
            return self._node(
                Literal, token.span, value=token.value, data_type=DataType.FLOAT
            )
        if self._check(TokenType.STRING_LITERAL):
            self._advance()
            return self._node(
                Literal, token.span, value=token.value, data_type=DataType.TEXT
            )

        if self._check_keyword(Keyword.TRUE, Keyword.FALSE):
            self._advance()
            return self._node(
                Literal,
                token.span,
                value=token.is_keyword(Keyword.TRUE),
                data_type=DataType.BOOLEAN,
            )
        if self._check_keyword(Keyword.NULL):
            self._advance()
            # NULL has no type until it is bound to a column, in Milestone 4.
            return self._node(Literal, token.span, value=None, data_type=None)

        if self._check(TokenType.IDENTIFIER):
            if self._peek_is_call():
                return self._function_call()
            return self._column_reference()

        if self._check(TokenType.STAR):
            self._fail(
                "'*' is only valid in a projection, not inside an expression",
                expected=("a column name", "a literal"),
            )

        self._fail(
            "expected a value",
            expected=("a column name", "a literal", "'('"),
        )

    def _peek_is_call(self) -> bool:
        """Is the identifier at the cursor followed by ``(``?

        What distinguishes ``count(x)`` from a column called ``count``. Keeping
        aggregate names out of the reserved set is why this lookahead exists,
        and it is the trade PostgreSQL and SQLite both make: a table with a
        ``min`` and a ``max`` column is not an unusual table.
        """
        return (
            self._pos + 1 < len(self._tokens)
            and self._tokens[self._pos + 1].type is TokenType.LPAREN
        )

    def _function_call(self) -> FunctionCall:
        name_token = self._advance()
        name = self._identifier_name(name_token).upper()
        try:
            function = AggregateFunction(name)
        except ValueError:
            self._fail(
                f"unknown function {self._identifier_name(name_token)!r}; ChenDB has "
                f"{', '.join(item.value for item in AggregateFunction)}",
                expected=tuple(item.value for item in AggregateFunction),
            )

        self._expect(TokenType.LPAREN, "'(' after a function name")
        argument: Expression | None = None
        if self._check(TokenType.STAR):
            # COUNT(*) counts rows; COUNT(x) counts rows where x is not NULL.
            # They are different questions and only one of them takes a column.
            star = self._advance()
            if function is not AggregateFunction.COUNT:
                self._fail(
                    f"{function.value}(*) is not meaningful; "
                    f"only COUNT counts rows rather than values"
                )
            del star
        else:
            argument = self._expression()
            if isinstance(argument, FunctionCall):
                self._unsupported("an aggregate of an aggregate is not allowed")
        end = self._expect(TokenType.RPAREN, "')' after the function argument").span

        return self._node(
            FunctionCall,
            name_token.span.union(end),
            function=function,
            argument=argument,
        )

    def _column_reference(self) -> ColumnRef:
        first = self._identifier("a column name")
        span = first.span
        table: str | None = None
        name = self._identifier_name(first)

        if self._match(TokenType.DOT):
            second = self._identifier("a column name after '.'")
            table = name
            name = self._identifier_name(second)
            span = span.union(second.span)

        return self._node(ColumnRef, span, name=name, table=table)


def _with_span[T: Node](node: T, span: SourceSpan) -> T:
    """A copy of ``node`` with a wider span. Nodes are frozen, so this replaces."""
    import dataclasses

    return dataclasses.replace(node, span=span)


def parse(source: str, *, tracer: Tracer | None = None) -> list[Statement]:
    """Tokenize and parse ``source`` into statements."""
    tokens = tokenize(source, tracer=tracer)
    return Parser(tokens, source, tracer=tracer).parse_script()


def parse_statement(source: str, *, tracer: Tracer | None = None) -> Statement:
    """Parse exactly one statement, rejecting anything after it."""
    statements = parse(source, tracer=tracer)
    if len(statements) != 1:
        raise ParseError(
            f"expected exactly one statement, found {len(statements)}",
            start=0,
            end=len(source),
        )
    return statements[0]
