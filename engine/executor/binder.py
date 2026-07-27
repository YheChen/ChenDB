"""Binding: resolving names in a parsed statement against a real schema.

The parser is purely syntactic — ``SELECT nope FROM nope`` parses perfectly
well.  Binding is the step that checks the statement against what actually
exists and rewrites it into a form the executor can run without doing any name
lookups:

    ColumnRef(name="age")   ──bind──▶   BoundColumnRef(name="age", column_index=2)

After binding, evaluating a column reference is one list index. That matters:
the alternative is a dictionary lookup per column per row, which on a million-row
scan is a million wasted hash computations.

PostgreSQL does the same thing in its ``transformStmt`` pass, turning a raw parse
tree into a ``Query`` with resolved ``Var`` nodes carrying ``varno``/``varattno``.
The parse tree it started from is thrown away.

Scope in Milestone 4
--------------------
Table names now resolve against the real :class:`~engine.catalog.catalog.Catalog`,
so ``SELECT * FROM nope`` fails here with the name of the table that does not
exist and a list of the ones that do.  Milestone 6 moves binding into a proper
front-end pass that also produces a logical plan; the *interface* here —
statement in, bound statement out, :class:`BindingError` on a name that does not
exist — is what survives.

Still missing: joins (one table per ``FROM``), so a qualified ``users.age`` is
accepted only when the qualifier names the table being scanned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.errors import BindingError, SchemaError
from engine.parser.ast import (
    BinaryOp,
    ColumnDefinition,
    ColumnRef,
    CreateTableStatement,
    Expression,
    InsertStatement,
    IsNullTest,
    Literal,
    SelectItem,
    SelectStatement,
    Star,
    Statement,
    UnaryOp,
)
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

if TYPE_CHECKING:
    from engine.catalog.catalog import Catalog, TableInfo

    #: Anything that can resolve a table name. A protocol in spirit; the concrete
    #: :class:`~engine.catalog.catalog.Catalog` is the only implementation.
    CatalogLike = Catalog

__all__ = [
    "BoundColumnRef",
    "BoundInsert",
    "BoundSelect",
    "BoundStatement",
    "ResultColumn",
    "bind_create_table",
    "bind_expression",
    "bind_insert",
    "bind_select",
]


@dataclass(frozen=True, slots=True)
class BoundColumnRef(Expression):
    """A column reference resolved to a positional index.

    Produced only by the binder — the parser never emits one. It is an
    ``Expression`` so it slots into an expression tree unchanged, and the generic
    ``children()``/``attributes()`` walk picks it up with no special casing.
    """

    name: str
    column_index: int
    data_type: DataType


@dataclass(frozen=True, slots=True)
class ResultColumn:
    """One column of a query's output."""

    name: str
    data_type: DataType | None
    """``None`` for an expression whose type is not statically known."""


@dataclass(frozen=True, slots=True)
class BoundSelect:
    """A ``SELECT`` with every name resolved."""

    table_name: str
    input_schema: Schema
    projections: tuple[Expression, ...]
    output_columns: tuple[ResultColumn, ...]
    where: Expression | None
    statement: SelectStatement

    @property
    def is_identity_projection(self) -> bool:
        """Whether the projection is every column, in order.

        When it is, the projection operator can be skipped entirely — a real
        optimisation the planner in Milestone 6 will generalise.
        """
        return all(
            isinstance(projection, BoundColumnRef) and projection.column_index == index
            for index, projection in enumerate(self.projections)
        ) and len(self.projections) == len(self.input_schema)


@dataclass(frozen=True, slots=True)
class BoundInsert:
    """An ``INSERT`` with its values reordered into schema order."""

    table_name: str
    schema: Schema
    rows: tuple[tuple[Expression, ...], ...]
    """One tuple per row, always in schema column order, always full width."""
    statement: InsertStatement


#: Anything the executor can run.
BoundStatement = BoundSelect | BoundInsert


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------


def bind_expression(expression: Expression, schema: Schema) -> Expression:
    """Resolve every column reference in ``expression`` against ``schema``."""
    match expression:
        case ColumnRef():
            return _bind_column_ref(expression, schema)

        case Literal():
            return expression

        case Star():
            raise BindingError(
                "'*' is only valid on its own in a projection",
                start=expression.span.start,
                end=expression.span.end,
                line=expression.span.line,
                column=expression.span.column,
            )

        case UnaryOp():
            return UnaryOp(
                node_id=expression.node_id,
                span=expression.span,
                operator=expression.operator,
                operand=bind_expression(expression.operand, schema),
            )

        case BinaryOp():
            return BinaryOp(
                node_id=expression.node_id,
                span=expression.span,
                operator=expression.operator,
                left=bind_expression(expression.left, schema),
                right=bind_expression(expression.right, schema),
            )

        case IsNullTest():
            return IsNullTest(
                node_id=expression.node_id,
                span=expression.span,
                operand=bind_expression(expression.operand, schema),
                negated=expression.negated,
            )

    # Already bound, or a node type a later milestone added.
    if isinstance(expression, BoundColumnRef):
        return expression
    raise BindingError(
        f"cannot bind {expression.node_type}",
        start=expression.span.start,
        end=expression.span.end,
        line=expression.span.line,
        column=expression.span.column,
    )


def _bind_column_ref(reference: ColumnRef, schema: Schema) -> BoundColumnRef:
    try:
        index = schema.index_of(reference.name)
    except SchemaError:
        raise BindingError(
            f"no column named {reference.name!r}; "
            f"this table has {', '.join(schema.column_names)}",
            start=reference.span.start,
            end=reference.span.end,
            line=reference.span.line,
            column=reference.span.column,
        ) from None

    return BoundColumnRef(
        node_id=reference.node_id,
        span=reference.span,
        name=schema[index].name,
        column_index=index,
        data_type=schema[index].data_type,
    )


def _static_type(expression: Expression) -> DataType | None:
    """The output type of an expression, when it can be known without a row.

    Comparisons and logical operators are BOOLEAN; a bound column has its
    declared type; a literal has its own. Arithmetic over mixed types and a bare
    ``NULL`` are left unknown rather than guessed.
    """
    match expression:
        case BoundColumnRef():
            return expression.data_type
        case Literal():
            return expression.data_type
        case IsNullTest():
            return DataType.BOOLEAN
        case UnaryOp():
            return _static_type(expression.operand)
        case BinaryOp() if (
            expression.operator.is_comparison or expression.operator.is_logical
        ):
            return DataType.BOOLEAN
        case BinaryOp():
            left = _static_type(expression.left)
            right = _static_type(expression.right)
            if left is right:
                return left
            if left in (DataType.INTEGER, DataType.FLOAT) and right in (
                DataType.INTEGER,
                DataType.FLOAT,
            ):
                return DataType.FLOAT
            return None
    return None


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


def bind_select(statement: SelectStatement, catalog: CatalogLike) -> BoundSelect:
    """Bind a ``SELECT`` against the catalog."""
    info = _resolve_table(statement.table.name, catalog, statement)
    table_name, schema = info.name, info.schema

    projections: list[Expression] = []
    output_columns: list[ResultColumn] = []

    for item in statement.projections:
        if isinstance(item.expression, Star):
            # `*` expands to every column, in declaration order. Done here so
            # the executor never sees a Star and the output width is fixed
            # before a single row is read.
            if item.alias is not None:
                raise BindingError(
                    "'*' cannot be aliased",
                    start=item.span.start,
                    end=item.span.end,
                    line=item.span.line,
                    column=item.span.column,
                )
            for index, column in enumerate(schema):
                projections.append(
                    BoundColumnRef(
                        node_id=item.expression.node_id,
                        span=item.expression.span,
                        name=column.name,
                        column_index=index,
                        data_type=column.data_type,
                    )
                )
                output_columns.append(ResultColumn(column.name, column.data_type))
            continue

        bound = bind_expression(item.expression, schema)
        projections.append(bound)
        output_columns.append(ResultColumn(_output_name(item, bound), _static_type(bound)))

    where = bind_expression(statement.where, schema) if statement.where else None

    return BoundSelect(
        table_name=table_name,
        input_schema=schema,
        projections=tuple(projections),
        output_columns=tuple(output_columns),
        where=where,
        statement=statement,
    )


def _output_name(item: SelectItem, bound: Expression) -> str:
    if item.alias:
        return item.alias
    if isinstance(bound, BoundColumnRef):
        return bound.name
    # PostgreSQL calls an unnamed expression "?column?". Being explicit that the
    # name is synthesised is better than inventing something that looks real.
    return "?column?"


def bind_insert(statement: InsertStatement, catalog: CatalogLike) -> BoundInsert:
    """Bind an ``INSERT``, reordering values into schema order.

    A statement may name its columns in any order and omit nullable ones. The
    executor should not have to care, so every row comes out of here full-width
    and in declaration order, with ``NULL`` literals filled in for omissions.
    """
    info = _resolve_table(statement.table.name, catalog, statement)
    table_name, schema = info.name, info.schema

    if statement.columns is None:
        target_indices = tuple(range(len(schema)))
    else:
        target_indices = _resolve_insert_columns(statement, schema)

    for row in statement.rows:
        if row.width != len(target_indices):
            raise BindingError(
                f"row has {row.width} values but {len(target_indices)} columns "
                f"are being inserted into",
                start=row.span.start,
                end=row.span.end,
                line=row.span.line,
                column=row.span.column,
            )

    _require_omitted_columns_are_nullable(statement, schema, target_indices)

    bound_rows: list[tuple[Expression, ...]] = []
    for row in statement.rows:
        # A NULL literal for each column the statement did not mention. It
        # shares the row's span so an error points at the right place.
        values: list[Expression] = [
            Literal(node_id=row.node_id, span=row.span, value=None, data_type=None)
            for _ in schema
        ]
        for position, column_index in enumerate(target_indices):
            values[column_index] = bind_expression(row.values[position], schema)
        bound_rows.append(tuple(values))

    return BoundInsert(
        table_name=table_name,
        schema=schema,
        rows=tuple(bound_rows),
        statement=statement,
    )


def _resolve_insert_columns(statement: InsertStatement, schema: Schema) -> tuple[int, ...]:
    assert statement.columns is not None
    indices: list[int] = []
    seen: set[int] = set()
    for name in statement.columns:
        try:
            index = schema.index_of(name)
        except SchemaError:
            raise BindingError(
                f"no column named {name!r}; "
                f"this table has {', '.join(schema.column_names)}",
                start=statement.table.span.start,
                end=statement.table.span.end,
                line=statement.table.span.line,
                column=statement.table.span.column,
            ) from None
        if index in seen:
            raise BindingError(
                f"column {schema[index].name!r} is named twice",
                start=statement.span.start,
                end=statement.span.end,
                line=statement.span.line,
                column=statement.span.column,
            )
        seen.add(index)
        indices.append(index)
    return tuple(indices)


def _require_omitted_columns_are_nullable(
    statement: InsertStatement, schema: Schema, target_indices: tuple[int, ...]
) -> None:
    """Catch a missing NOT NULL column here rather than at encode time.

    The encoder would reject it too, but only with the column name — no source
    position, and only once the statement was already half-executed.
    """
    omitted = set(range(len(schema))) - set(target_indices)
    missing = [
        schema[index].name for index in sorted(omitted) if not schema[index].nullable
    ]
    if missing:
        raise BindingError(
            f"column(s) {', '.join(repr(name) for name in missing)} are NOT NULL "
            f"but no value was supplied",
            start=statement.span.start,
            end=statement.span.end,
            line=statement.span.line,
            column=statement.span.column,
        )


def bind_create_table(statement: CreateTableStatement) -> tuple[str, Schema]:
    """Turn a ``CREATE TABLE`` into a name and a :class:`Schema`.

    Not really *binding* — there is nothing to resolve against — but it belongs
    with the other AST-to-engine translations, and it is where a bad column
    definition gets a source position attached.
    """
    columns: list[Column] = []
    for definition in statement.columns:
        columns.append(_column_from(definition))
    try:
        return statement.table.name, Schema(tuple(columns))
    except SchemaError as exc:
        raise BindingError(
            str(exc),
            start=statement.span.start,
            end=statement.span.end,
            line=statement.span.line,
            column=statement.span.column,
        ) from None


def _column_from(definition: ColumnDefinition) -> Column:
    try:
        return Column(
            name=definition.name,
            data_type=definition.data_type,
            nullable=not definition.not_null,
            primary_key=definition.primary_key,
        )
    except SchemaError as exc:
        raise BindingError(
            str(exc),
            start=definition.span.start,
            end=definition.span.end,
            line=definition.span.line,
            column=definition.span.column,
        ) from None


def _resolve_table(
    referenced: str, catalog: CatalogLike, statement: Statement
) -> TableInfo:
    """Look up a table, turning a miss into a positioned :class:`BindingError`.

    The message lists what *does* exist, because "no such table" without that is
    the least helpful error a database can give.
    """
    info = catalog.get_table(referenced)
    if info is not None:
        return info

    known = ", ".join(table.name for table in catalog.list_tables())
    detail = f"this database has {known}" if known else "this database has no tables"
    raise BindingError(
        f"no table named {referenced!r}; {detail}",
        start=statement.table.span.start
        if isinstance(statement, SelectStatement | InsertStatement)
        else statement.span.start,
        end=statement.table.span.end
        if isinstance(statement, SelectStatement | InsertStatement)
        else statement.span.end,
        line=statement.table.span.line
        if isinstance(statement, SelectStatement | InsertStatement)
        else statement.span.line,
        column=statement.table.span.column
        if isinstance(statement, SelectStatement | InsertStatement)
        else statement.span.column,
    )
