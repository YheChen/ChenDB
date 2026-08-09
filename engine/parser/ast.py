"""The abstract syntax tree.

Every node is a frozen dataclass carrying two things beyond its own contents:

* ``node_id``. A per-parse sequence number, so the visualizer can address a
  node and the parser can report events about it;
* ``span``. The character range of the source it was built from, spanning
  *all* the tokens involved. Selecting a ``BinaryOp`` in the UI therefore
  highlights ``age >= 18``, not just the ``>=``.

Tree walking is generic
-----------------------
:meth:`Node.children` and :meth:`Node.attributes` are implemented once on the
base by introspecting dataclass fields.  Nothing needs a visitor per node type,
and adding a node in a later milestone needs no change to the mapper, the
renderer, or the tests that walk the tree.

    SELECT name FROM users WHERE age >= 18
    │
    └─ SelectStatement                        [0:37]
       ├─ SelectItem                          [7:11]
       │  └─ ColumnRef      name              [7:11]
       ├─ TableRef          users             [17:22]
       └─ BinaryOp          >=                [29:37]
          ├─ ColumnRef      age               [29:32]
          └─ Literal        18                [36:38]

Why an AST at all
-----------------
The alternative is executing while parsing, which some very small
interpreters do. It fails as soon as you need to *inspect* a query before
running it: no rewriting, no cost-based planning, no `EXPLAIN`, no binding
against a catalog. PostgreSQL parses to a raw parse tree, transforms it into a
``Query``, then plans; SQLite parses straight into bytecode but keeps an
``Expr`` tree for expressions for exactly this reason.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from engine.parser.tokens import SourceSpan
from engine.serialization.types import DataType

__all__ = [
    "AggregateFunction",
    "AnalyzeStatement",
    "Assignment",
    "BeginStatement",
    "BinaryOp",
    "BinaryOperator",
    "ColumnConstraint",
    "ColumnDefinition",
    "ColumnRef",
    "CommitStatement",
    "CreateIndexStatement",
    "CreateTableStatement",
    "DeleteStatement",
    "ExplainStatement",
    "Expression",
    "FunctionCall",
    "InList",
    "InsertStatement",
    "IsNullTest",
    "JoinClause",
    "JoinKind",
    "Literal",
    "Node",
    "OrderByItem",
    "RollbackStatement",
    "ScalarSubquery",
    "SelectItem",
    "SelectStatement",
    "SortDirection",
    "Star",
    "Statement",
    "TableRef",
    "UnaryOp",
    "UnaryOperator",
    "UpdateStatement",
    "ValuesRow",
    "walk",
]


@dataclass(frozen=True, slots=True)
class Node:
    """Base class for every AST node."""

    node_id: int
    span: SourceSpan

    @property
    def node_type(self) -> str:
        """The class name, used as the display label and API discriminator."""
        return type(self).__name__

    def children(self) -> tuple[Node, ...]:
        """Child nodes in declaration order.

        Derived from the dataclass fields, so a new node type is walkable the
        moment it is declared. Tuples of nodes are flattened, which covers
        every list-shaped field in the grammar (projections, column
        definitions, value rows).
        """
        found: list[Node] = []
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Node):
                found.append(value)
            elif isinstance(value, tuple):
                found.extend(item for item in value if isinstance(item, Node))
        return tuple(found)

    def attributes(self) -> dict[str, Any]:
        """Scalar fields worth showing, excluding ``node_id`` and ``span``.

        Enums are rendered by value and nested nodes are omitted. They are
        already reachable through :meth:`children`.
        """
        out: dict[str, Any] = {}
        for field in dataclasses.fields(self):
            if field.name in ("node_id", "span"):
                continue
            value = getattr(self, field.name)
            if isinstance(value, Node):
                continue
            if isinstance(value, tuple):
                if any(isinstance(item, Node) for item in value):
                    continue
                out[field.name] = list(value)
            elif isinstance(value, StrEnum):
                out[field.name] = value.value
            elif isinstance(value, DataType):
                out[field.name] = value.sql_name
            else:
                out[field.name] = value
        return out

    def text_in(self, source: str) -> str:
        """The source fragment this node was parsed from."""
        return self.span.text_in(source)


def walk(node: Node) -> list[Node]:
    """Every node in the subtree, parents before children (pre-order)."""
    out = [node]
    for child in node.children():
        out.extend(walk(child))
    return out


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expression(Node):
    """Anything that produces a value."""


class BinaryOperator(StrEnum):
    """Infix operators, spelled as they appear in SQL."""

    EQ = "="
    NEQ = "<>"
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "*"
    DIVIDE = "/"
    MODULO = "%"
    AND = "AND"
    OR = "OR"

    @property
    def is_comparison(self) -> bool:
        return self in _COMPARISONS

    @property
    def is_logical(self) -> bool:
        return self in (BinaryOperator.AND, BinaryOperator.OR)


_COMPARISONS = frozenset(
    {
        BinaryOperator.EQ,
        BinaryOperator.NEQ,
        BinaryOperator.LT,
        BinaryOperator.LTE,
        BinaryOperator.GT,
        BinaryOperator.GTE,
    }
)


class UnaryOperator(StrEnum):
    NOT = "NOT"
    NEGATE = "-"
    PLUS = "+"


@dataclass(frozen=True, slots=True)
class Literal(Expression):
    """A constant. ``value`` is ``None`` for SQL ``NULL``."""

    value: Any
    data_type: DataType | None
    """``None`` only for ``NULL``, which has no type until it is bound."""


@dataclass(frozen=True, slots=True)
class ColumnRef(Expression):
    """A column reference, optionally qualified: ``age`` or ``users.age``."""

    name: str
    table: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True, slots=True)
class Star(Expression):
    """``*`` in a projection, or ``u.*`` for one table of a join.

    An expression rather than a flag on ``SelectStatement`` so that
    ``SELECT *, id`` parses, and so the projection list stays homogeneous for
    the planner to walk.
    """

    table: str | None = None


@dataclass(frozen=True, slots=True)
class UnaryOp(Expression):
    operator: UnaryOperator
    operand: Expression


@dataclass(frozen=True, slots=True)
class BinaryOp(Expression):
    operator: BinaryOperator
    left: Expression
    right: Expression


@dataclass(frozen=True, slots=True)
class InList(Expression):
    """``x IN (a, b, c)``, and ``x NOT IN (…)``.

    Kept as its own node rather than desugared at parse time into
    ``x = a OR x = b``. The desugaring is *exactly* correct, NULLs included, and
    it would still be the wrong thing to store: the AST view is meant to show
    the query somebody wrote, and an error span pointing at an ``OR`` nobody
    typed is worse than the node costing an evaluator case.

    The NULL behaviour that surprises people falls out of the equivalence rather
    than being coded: ``x NOT IN (1, NULL)`` is never TRUE, because it means
    ``x <> 1 AND x <> NULL`` and the second is always unknown.
    """

    operand: Expression
    items: tuple[Expression, ...]
    negated: bool = False


@dataclass(frozen=True, slots=True)
class IsNullTest(Expression):
    """``x IS NULL`` / ``x IS NOT NULL``.

    Its own node rather than ``BinaryOp(EQ, x, Literal(None))`` because
    ``x = NULL`` is *not* the same thing: in three-valued logic it evaluates to
    UNKNOWN for every input, including NULL. Conflating them is one of the
    classic SQL bugs, and keeping them distinct in the AST makes that
    impossible here.
    """

    operand: Expression
    negated: bool


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Statement(Node):
    """A complete SQL statement."""


@dataclass(frozen=True, slots=True)
class TableRef(Node):
    """A table named in a statement, optionally under an alias.

    The alias is what a qualified column resolves against, not the table name:
    ``FROM users u`` makes ``u.id`` valid and ``users.id`` an error, which is
    what every SQL engine does and what makes a self-join expressible at all.
    """

    name: str
    alias: str | None = None

    @property
    def binding_name(self) -> str:
        """What a qualified reference must use to reach this table."""
        return self.alias or self.name


class ColumnConstraint(StrEnum):
    NOT_NULL = "NOT NULL"
    NULL = "NULL"
    PRIMARY_KEY = "PRIMARY KEY"
    UNIQUE = "UNIQUE"


@dataclass(frozen=True, slots=True)
class ColumnDefinition(Node):
    """One column inside ``CREATE TABLE``."""

    name: str
    data_type: DataType
    constraints: tuple[ColumnConstraint, ...] = ()

    @property
    def not_null(self) -> bool:
        return (
            ColumnConstraint.NOT_NULL in self.constraints
            or ColumnConstraint.PRIMARY_KEY in self.constraints
        )

    @property
    def primary_key(self) -> bool:
        return ColumnConstraint.PRIMARY_KEY in self.constraints


@dataclass(frozen=True, slots=True)
class CreateTableStatement(Statement):
    table: TableRef
    columns: tuple[ColumnDefinition, ...]
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class BeginStatement(Statement):
    """``BEGIN``, from here, writes can be taken back.

    No isolation level: there is one writer, so there is nothing to be isolated
    *from*. ``READ COMMITTED`` and friends arrive with MVCC in Milestone 10.
    """


@dataclass(frozen=True, slots=True)
class CommitStatement(Statement):
    """``COMMIT``, accept the work and discard the undo log."""


@dataclass(frozen=True, slots=True)
class RollbackStatement(Statement):
    """``ROLLBACK``, put every touched page back as it was."""


@dataclass(frozen=True, slots=True)
class ExplainStatement(Statement):
    """``EXPLAIN [ANALYZE] <statement>``.

    Wraps another statement rather than being a flag on it, so nothing
    downstream has to check "am I being explained?", the executor branches
    once, at the top.

    ``analyze`` here means PostgreSQL's ``EXPLAIN ANALYZE``: *run* the query and
    report actual rows beside the estimates. It is unrelated to the ``ANALYZE``
    statement, which gathers statistics. Sharing the word is SQL's fault, and
    the collision is worth knowing about because the two do opposite things,
    one executes, the other only measures.
    """

    statement: Statement
    analyze: bool = False


@dataclass(frozen=True, slots=True)
class AnalyzeStatement(Statement):
    """``ANALYZE [table]``, recompute statistics. Omit the name for every table."""

    table: TableRef | None = None


@dataclass(frozen=True, slots=True)
class CreateIndexStatement(Statement):
    """``CREATE [UNIQUE] INDEX name ON table (column)``.

    One column only. Multi-column indexes need a composite key encoding, which
    :mod:`engine.index.key` explains is a whole escaping layer on its own; the
    grammar rejects a second column rather than silently indexing the first.
    """

    index_name: str
    table: TableRef
    column: str
    unique: bool = False
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class ValuesRow(Node):
    """One ``(...)`` group in an ``INSERT ... VALUES`` clause.

    A node rather than a bare tuple of expressions for two reasons: a row is a
    real syntactic construct with its own span, so the UI can highlight
    ``(1, 'Ada')`` as a unit; and it keeps the invariant that every field of a
    node is a scalar, a ``Node``, or a tuple of ``Node``. Nesting tuples inside
    tuples would make the generic tree walk silently skip the values.
    """

    values: tuple[Expression, ...]

    @property
    def width(self) -> int:
        return len(self.values)


@dataclass(frozen=True, slots=True)
class InsertStatement(Statement):
    table: TableRef
    columns: tuple[str, ...] | None
    """``None`` when the statement omits the column list, meaning "all columns
    in declaration order"."""
    rows: tuple[ValuesRow, ...]


@dataclass(frozen=True, slots=True)
class Assignment(Node):
    """One ``column = expression`` inside ``UPDATE ... SET``.

    A node rather than a ``(str, Expression)`` pair so the value is reachable
    through the generic tree walk, and so the UI can highlight ``price = price *
    2`` as a unit. The left side is a bare name, not a :class:`ColumnRef`: SQL
    does not let you assign to ``other_table.col`` or to an expression, and
    modelling the target as something evaluable would invite exactly that.
    """

    column: str
    value: Expression


@dataclass(frozen=True, slots=True)
class UpdateStatement(Statement):
    """``UPDATE table SET col = expr [, ...] [ WHERE expr ]``.

    Note what is *not* here: a ``FROM`` clause. PostgreSQL's ``UPDATE ... FROM``
    and the standard's ``UPDATE ... WHERE CURRENT OF`` both need a second row
    source, and there are no joins yet.
    """

    table: TableRef
    assignments: tuple[Assignment, ...]
    where: Expression | None = None


@dataclass(frozen=True, slots=True)
class DeleteStatement(Statement):
    """``DELETE FROM table [ WHERE expr ]``.

    ``where`` of ``None`` means every row, which is the correct reading and also
    the most expensive mistake in SQL. Nothing here second-guesses it (a
    confirmation prompt belongs in a client, not a grammar) but the executor's
    message says how many rows went, so the mistake is at least visible.
    """

    table: TableRef
    where: Expression | None = None


@dataclass(frozen=True, slots=True)
class SelectItem(Node):
    """One entry in a projection list, with an optional alias."""

    expression: Expression
    alias: str | None = None

    @property
    def output_name(self) -> str:
        """The column name this item produces."""
        if self.alias:
            return self.alias
        if isinstance(self.expression, ColumnRef):
            return self.expression.name
        if isinstance(self.expression, Star):
            return "*"
        # An unnamed expression. PostgreSQL would call this "?column?".
        return self.expression.node_type.lower()


class JoinKind(StrEnum):
    """How unmatched rows are treated. The one thing a join's *name* decides.

    ``INNER`` drops a row with no partner. The other three keep it and fill the
    missing side with NULLs, ``LEFT`` preserves the rows written to the left of
    the keyword, ``RIGHT`` those to its right, ``FULL`` both.

    This is not a flag on a common case. An inner join is commutative and
    associative, which is the entire licence the planner has to reorder; an outer
    join is neither, so :func:`~engine.planner.physical._plan_joins` has to treat
    one as a barrier rather than as another relation in the search.
    """

    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FULL = "FULL"

    @property
    def is_outer(self) -> bool:
        return self is not JoinKind.INNER

    @property
    def preserves_left(self) -> bool:
        """Whether a row written *before* the keyword survives with no partner."""
        return self in (JoinKind.LEFT, JoinKind.FULL)

    @property
    def preserves_right(self) -> bool:
        """Whether a row written *after* the keyword survives with no partner."""
        return self in (JoinKind.RIGHT, JoinKind.FULL)

    @classmethod
    def of(cls, *, preserve_left: bool, preserve_right: bool) -> JoinKind:
        """The kind with exactly these two behaviours. The inverse of the pair above.

        A join *is* those two booleans; the four names are how SQL spells the
        four combinations. Naming the inverse is what lets a rewrite drop one
        behaviour without a table of special cases: proving that a ``FULL``
        join's left-preserved rows all die turns it into a ``RIGHT``, and the
        same line turns a ``LEFT`` into an ``INNER``.
        """
        match (preserve_left, preserve_right):
            case (True, True):
                return cls.FULL
            case (True, False):
                return cls.LEFT
            case (False, True):
                return cls.RIGHT
            case _:
                return cls.INNER


@dataclass(frozen=True, slots=True)
class JoinClause(Node):
    """One ``JOIN b ON …`` appended to a ``FROM``.

    A flat list of these rather than a nested tree, because for an *inner* join
    the written order is not the order it runs in. The planner reorders freely,
    and a tree here would suggest a nesting the user does not control. That is
    what the SQL standard means by joins being commutative and associative, and
    the whole reason join ordering is a search problem.

    An outer join is neither commutative nor associative, so for one the written
    order *is* meaningful. The list stays flat anyway: ``kind`` records the
    constraint and the planner honours it, which keeps one representation for both
    rather than a tree for outer joins and a list for inner ones.
    """

    table: TableRef
    on: Expression
    kind: JoinKind = JoinKind.INNER


class SortDirection(StrEnum):
    ASC = "ASC"
    DESC = "DESC"


@dataclass(frozen=True, slots=True)
class OrderByItem(Node):
    """One ``expr [ASC|DESC]`` in an ``ORDER BY``."""

    expression: Expression
    direction: SortDirection = SortDirection.ASC


class AggregateFunction(StrEnum):
    COUNT = "COUNT"
    SUM = "SUM"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"


@dataclass(frozen=True, slots=True)
class FunctionCall(Expression):
    """``COUNT(*)``, ``SUM(price)``, and nothing else yet.

    ``argument`` is ``None`` for ``COUNT(*)``, which is not sugar for
    ``COUNT(1)``: the star form counts *rows*, and the expression form counts
    rows where the expression is not NULL. Conflating them is the most common
    SQL misunderstanding there is, and keeping the distinction in the AST makes
    it impossible here.
    """

    function: AggregateFunction
    argument: Expression | None

    @property
    def is_star(self) -> bool:
        return self.argument is None


@dataclass(frozen=True, slots=True)
class SelectStatement(Statement):
    projections: tuple[SelectItem, ...]
    table: TableRef
    distinct: bool = False
    """``SELECT DISTINCT``: emit each *output row* once.

    A property of the whole select rather than of a projection, because that is
    what it means. ``SELECT DISTINCT a, b`` deduplicates the pair, not each
    column, and ``COUNT(DISTINCT x)`` is a different feature that lives inside
    an aggregate.
    """
    joins: tuple[JoinClause, ...] = ()
    where: Expression | None = None
    group_by: tuple[Expression, ...] = ()
    having: Expression | None = None
    order_by: tuple[OrderByItem, ...] = ()
    limit: int | None = None
    offset: int | None = None

    @property
    def is_select_star(self) -> bool:
        return len(self.projections) == 1 and isinstance(
            self.projections[0].expression, Star
        )

    @property
    def tables(self) -> tuple[TableRef, ...]:
        """Every table in the ``FROM``, in the order it was written."""
        return (self.table, *(join.table for join in self.joins))


@dataclass(frozen=True, slots=True)
class ScalarSubquery(Expression):
    """``(SELECT …)`` used where a value is expected.

    Declared after :class:`SelectStatement` because it holds one, which is the
    only cycle in this grammar and the reason the file is ordered the way it is.

    A subquery that names nothing outside itself is a **constant**: it depends
    on no row of the query around it, so it has one value for the whole
    statement however many rows that statement scans. Milestone 23 runs it once
    and substitutes the value, which is both the simplest correct thing and,
    for this shape, the fastest. A *correlated* subquery is a different feature
    with a different implementation (a join, or a per-row re-execution) and the
    parser refuses one by name rather than running it slowly.
    """

    statement: SelectStatement

    @property
    def detail(self) -> str:
        return "(SELECT …)"
