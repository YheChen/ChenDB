"""The abstract syntax tree.

Every node is a frozen dataclass carrying two things beyond its own contents:

* ``node_id`` — a per-parse sequence number, so the visualizer can address a
  node and the parser can report events about it;
* ``span`` — the character range of the source it was built from, spanning
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
    "AnalyzeStatement",
    "BinaryOp",
    "BinaryOperator",
    "ColumnConstraint",
    "ColumnDefinition",
    "ColumnRef",
    "CreateIndexStatement",
    "CreateTableStatement",
    "ExplainStatement",
    "Expression",
    "InsertStatement",
    "IsNullTest",
    "Literal",
    "Node",
    "SelectItem",
    "SelectStatement",
    "Star",
    "Statement",
    "TableRef",
    "UnaryOp",
    "UnaryOperator",
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

        Enums are rendered by value and nested nodes are omitted — they are
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
    """``*`` in a projection.

    An expression rather than a flag on ``SelectStatement`` so that
    ``SELECT *, id`` parses, and so the projection list stays homogeneous for
    the planner to walk.
    """


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
    """A table named in a statement."""

    name: str


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
class ExplainStatement(Statement):
    """``EXPLAIN [ANALYZE] <statement>``.

    Wraps another statement rather than being a flag on it, so nothing
    downstream has to check "am I being explained?" — the executor branches
    once, at the top.

    ``analyze`` here means PostgreSQL's ``EXPLAIN ANALYZE``: *run* the query and
    report actual rows beside the estimates. It is unrelated to the ``ANALYZE``
    statement, which gathers statistics. Sharing the word is SQL's fault, and
    the collision is worth knowing about because the two do opposite things —
    one executes, the other only measures.
    """

    statement: Statement
    analyze: bool = False


@dataclass(frozen=True, slots=True)
class AnalyzeStatement(Statement):
    """``ANALYZE [table]`` — recompute statistics. Omit the name for every table."""

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


@dataclass(frozen=True, slots=True)
class SelectStatement(Statement):
    projections: tuple[SelectItem, ...]
    table: TableRef
    where: Expression | None = None

    @property
    def is_select_star(self) -> bool:
        return len(self.projections) == 1 and isinstance(
            self.projections[0].expression, Star
        )
