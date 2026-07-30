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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from engine.errors import BindingError, SchemaError
from engine.parser.ast import (
    AggregateFunction,
    Assignment,
    BinaryOp,
    ColumnDefinition,
    ColumnRef,
    CreateTableStatement,
    DeleteStatement,
    Expression,
    FunctionCall,
    InsertStatement,
    IsNullTest,
    JoinKind,
    Literal,
    SelectItem,
    SelectStatement,
    SortDirection,
    Star,
    Statement,
    TableRef,
    UnaryOp,
    UpdateStatement,
    walk,
)
from engine.parser.tokens import SourceSpan
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

if TYPE_CHECKING:
    from engine.catalog.catalog import Catalog, TableInfo

    #: Anything that can resolve a table name. A protocol in spirit; the concrete
    #: :class:`~engine.catalog.catalog.Catalog` is the only implementation.
    CatalogLike = Catalog

__all__ = [
    "BoundAggregate",
    "BoundAggregation",
    "BoundAssignment",
    "BoundColumnRef",
    "BoundDelete",
    "BoundInsert",
    "BoundJoin",
    "BoundSelect",
    "BoundSortKey",
    "BoundStatement",
    "BoundUpdate",
    "ResultColumn",
    "RowLayout",
    "Scope",
    "ScopeEntry",
    "bind_create_table",
    "bind_delete",
    "bind_expression",
    "bind_insert",
    "bind_select",
    "bind_update",
    "build_scope",
    "identity_projection",
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
    """Index into the row this expression will be evaluated against — the
    *joined* row below an aggregate, the *grouped* row above one."""
    data_type: DataType
    table: str | None = None
    """The binding name it came from, for display. ``None`` above an aggregate,
    where the index addresses a grouped row rather than a joined one."""
    scan_position: int | None = None
    """Which table in the ``FROM``, by position. The planner reads this to tell
    a single-table predicate from a join condition without a scope in hand."""
    table_name: str | None = None
    """The real table, not the alias — what the statistics are keyed on."""
    table_position: int | None = None
    """The column's index within its own table, for a statistics lookup."""

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True, slots=True)
class ResultColumn:
    """One column of a query's output."""

    name: str
    data_type: DataType | None
    """``None`` for an expression whose type is not statically known."""


@dataclass(frozen=True, slots=True)
class BoundJoin:
    """One join in a ``FROM``, resolved.

    Names the table by its *binding* — the alias if it has one — rather than
    holding a subtree, because the planner is free to reorder and a tree here
    would imply an order the user does not control.
    """

    binding_name: str
    condition: Expression
    """Bound against the whole scope, so both sides are already flat indices."""
    kind: JoinKind = JoinKind.INNER
    """``INNER`` unless the join preserves unmatched rows. The planner's licence
    to reorder comes from this being ``INNER``, so it has to travel."""


@dataclass(frozen=True, slots=True)
class BoundAggregate:
    """One aggregate call, and where its result lands in the grouped row."""

    function: AggregateFunction
    argument: Expression | None
    """``None`` for ``COUNT(*)``. Bound against the *input* row."""
    slot: int
    label: str

    @property
    def counts_rows(self) -> bool:
        return self.function is AggregateFunction.COUNT and self.argument is None


@dataclass(frozen=True, slots=True)
class BoundAggregation:
    """A ``GROUP BY``, or a bare aggregate over the whole input.

    The grouped row is laid out ``[key₀ … keyₖ₋₁, agg₀ … aggₘ₋₁]``, and every
    projection above it has been rewritten to index into *that*. So the
    projection operator does not know it is sitting on an aggregate: it reads a
    list of values by position, as it always has.
    """

    group_keys: tuple[Expression, ...]
    aggregates: tuple[BoundAggregate, ...]
    having: Expression | None
    """Bound against the grouped row, not the input one."""

    @property
    def is_scalar(self) -> bool:
        """``SELECT COUNT(*) FROM t`` — one group, always, even over no rows."""
        return not self.group_keys

    @property
    def width(self) -> int:
        return len(self.group_keys) + len(self.aggregates)


@dataclass(frozen=True, slots=True)
class BoundSortKey:
    """One ``ORDER BY`` term, as an index into the *projected* row."""

    output_index: int
    descending: bool


@dataclass(frozen=True, slots=True)
class BoundSelect:
    """A ``SELECT`` with every name resolved."""

    table_name: str
    """The first table in the ``FROM``. Kept for callers written before joins."""
    input_schema: Schema
    """The first table's schema, for the same reason."""
    scope: Scope
    joins: tuple[BoundJoin, ...]
    projections: tuple[Expression, ...]
    output_columns: tuple[ResultColumn, ...]
    where: Expression | None
    aggregation: BoundAggregation | None
    order_by: tuple[BoundSortKey, ...]
    limit: int | None
    offset: int | None
    statement: SelectStatement

    @property
    def is_identity_projection(self) -> bool:
        """Whether the projection is every column of the input, in order.

        When it is, the projection operator can be skipped entirely — a real
        optimisation :mod:`engine.optimizer.rules` applies. Never true above an
        aggregate, whose output row is a different shape from its input.
        """
        if self.aggregation is not None:
            return False
        return (
            all(
                isinstance(projection, BoundColumnRef) and projection.column_index == index
                for index, projection in enumerate(self.projections)
            )
            and len(self.projections) == self.scope.width
        )


@dataclass(frozen=True, slots=True)
class BoundInsert:
    """An ``INSERT`` with its values reordered into schema order."""

    table_name: str
    schema: Schema
    rows: tuple[tuple[Expression, ...], ...]
    """One tuple per row, always in schema column order, always full width."""
    statement: InsertStatement


@dataclass(frozen=True, slots=True)
class BoundAssignment:
    """One ``SET`` target, resolved to a column position."""

    column_index: int
    column: Column
    value: Expression
    """Bound against the table's own schema, so it can read the row it is
    replacing: ``SET n = n + 1`` is evaluated over the *old* version."""


@dataclass(frozen=True, slots=True)
class BoundUpdate:
    """An ``UPDATE`` with its targets resolved and its values bound."""

    table_name: str
    schema: Schema
    assignments: tuple[BoundAssignment, ...]
    where: Expression | None
    statement: UpdateStatement


@dataclass(frozen=True, slots=True)
class BoundDelete:
    """A ``DELETE`` with its table resolved."""

    table_name: str
    schema: Schema
    where: Expression | None
    statement: DeleteStatement


#: Anything the executor can run.
BoundStatement = BoundSelect | BoundInsert | BoundUpdate | BoundDelete


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------


def bind_expression(expression: Expression, scope: Scope | Schema) -> Expression:
    """Resolve every column reference in ``expression`` against ``scope``.

    A bare :class:`Schema` is accepted and wrapped, so the single-table callers
    written before joins existed — ``INSERT``, ``UPDATE``, ``DELETE`` — read the
    same as they did.
    """
    if isinstance(scope, Schema):
        scope = Scope.of(scope)

    match expression:
        case ColumnRef():
            return _bind_column_ref(expression, scope)

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

        case FunctionCall():
            return FunctionCall(
                node_id=expression.node_id,
                span=expression.span,
                function=expression.function,
                argument=(
                    None
                    if expression.argument is None
                    else bind_expression(expression.argument, scope)
                ),
            )

        case UnaryOp():
            return UnaryOp(
                node_id=expression.node_id,
                span=expression.span,
                operator=expression.operator,
                operand=bind_expression(expression.operand, scope),
            )

        case BinaryOp():
            return BinaryOp(
                node_id=expression.node_id,
                span=expression.span,
                operator=expression.operator,
                left=bind_expression(expression.left, scope),
                right=bind_expression(expression.right, scope),
            )

        case IsNullTest():
            return IsNullTest(
                node_id=expression.node_id,
                span=expression.span,
                operand=bind_expression(expression.operand, scope),
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


def _bind_column_ref(reference: ColumnRef, scope: Scope) -> BoundColumnRef:
    """Resolve a name to one flat index, refusing an ambiguous one.

    ``users.age`` looks in one table; ``age`` looks in all of them and fails if
    more than one has it. Deciding that here rather than at run time is what
    makes a join of two tables that both have ``id`` an error you can read
    instead of a value you cannot predict.
    """
    # Case-insensitive, like `Schema.index_of` and like SQL: an unquoted
    # identifier is folded. The *declared* case is what comes back, so the
    # result header says what CREATE TABLE said.
    wanted = reference.name.casefold()
    qualifier = reference.table.casefold() if reference.table is not None else None
    candidates = [
        (entry, index, column)
        for entry, index, column in scope.columns()
        if column.name.casefold() == wanted
        and (qualifier is None or entry.binding_name.casefold() == qualifier)
    ]

    if not candidates:
        raise BindingError(
            _no_such_column(reference, scope),
            start=reference.span.start,
            end=reference.span.end,
            line=reference.span.line,
            column=reference.span.column,
        )
    if len(candidates) > 1:
        where = ", ".join(
            f"{entry.binding_name}.{column.name}" for entry, _, column in candidates
        )
        raise BindingError(
            f"{reference.name!r} is ambiguous: it could be {where}",
            start=reference.span.start,
            end=reference.span.end,
            line=reference.span.line,
            column=reference.span.column,
        )

    entry, index, column = candidates[0]
    return BoundColumnRef(
        node_id=reference.node_id,
        span=reference.span,
        name=column.name,
        column_index=index,
        data_type=column.data_type,
        table=entry.binding_name,
        scan_position=entry.position,
        table_name=entry.table_name,
        table_position=index - entry.offset,
    )


def _no_such_column(reference: ColumnRef, scope: Scope) -> str:
    """A message that names what *does* exist, and the likely cause."""
    if reference.table is not None and scope.entry(reference.table) is None:
        known = ", ".join(entry.binding_name for entry in scope.entries)
        aliased = any(entry.binding_name != entry.table_name for entry in scope.entries)
        return f"no table named {reference.table!r} in FROM; this query has {known}" + (
            " — note that an alias replaces the table name" if aliased else ""
        )
    available = ", ".join(
        column.name if scope.is_single_table else f"{entry.binding_name}.{column.name}"
        for entry, _, column in scope.columns()
    )
    return f"no column named {reference.qualified_name!r}; this query has {available}"


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
        case FunctionCall():
            return _aggregate_type(expression.function, expression.argument)
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


def _aggregate_type(
    function: AggregateFunction, argument: Expression | None
) -> DataType | None:
    """What an aggregate produces.

    ``COUNT`` is always an integer however you spell it. ``AVG`` is always a
    float, including over integers — ``AVG(age)`` of 1 and 2 is 1.5, and
    truncating it because the column was INTEGER is the kind of quiet
    wrongness this project exists to avoid. ``SUM``, ``MIN`` and ``MAX`` keep
    the input's type.
    """
    if function is AggregateFunction.COUNT:
        return DataType.INTEGER
    if function is AggregateFunction.AVG:
        return DataType.FLOAT
    return _static_type(argument) if argument is not None else None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _signature(expression: Expression) -> Any:
    """A structural key ignoring ``node_id`` and ``span``.

    ``GROUP BY u.name`` and the ``u.name`` in the select list are different
    nodes from different places in the source, and they have to compare equal
    or the second would be rejected as "not in GROUP BY". Comparing the
    dataclasses directly would not work: they carry the position they were
    parsed from.
    """
    match expression:
        case BoundColumnRef():
            return ("col", expression.column_index)
        case Literal():
            return ("lit", expression.value, expression.data_type)
        case FunctionCall():
            return (
                "agg",
                expression.function,
                None if expression.argument is None else _signature(expression.argument),
            )
        case UnaryOp():
            return ("un", expression.operator, _signature(expression.operand))
        case BinaryOp():
            return (
                "bin",
                expression.operator,
                _signature(expression.left),
                _signature(expression.right),
            )
        case IsNullTest():
            return ("null", expression.negated, _signature(expression.operand))
    return ("other", id(expression))  # pragma: no cover - every node is covered


def _any_aggregate(expression: Expression | None) -> bool:
    return expression is not None and any(
        isinstance(node, FunctionCall) for node in walk(expression)
    )


def _reject_aggregates(expression: Expression, where: str, *, hint: str = "") -> None:
    found = next(
        (node for node in walk(expression) if isinstance(node, FunctionCall)), None
    )
    if found is None:
        return
    assert isinstance(found, FunctionCall)
    raise BindingError(
        f"{found.function.value} is an aggregate and cannot appear in {where}"
        + (f"; {hint}" if hint else ""),
        start=found.span.start,
        end=found.span.end,
        line=found.span.line,
        column=found.span.column,
    )


def _require_predicate(expression: Expression, clause: str, *, hint: str = "") -> None:
    """Refuse a condition that is not a boolean, when the type is already known.

    ``WHERE v`` over an INTEGER column is not a condition, and until Milestone 17
    it silently matched nothing: the filter asked ``value is True``, ``5`` is not,
    and a rejected row looks exactly like a row that failed a test. The runtime
    now raises (:func:`~engine.executor.expression.is_true`), but the honest
    place to say so is here, before a page is read — and a bind error carries the
    span, so the editor underlines the offending expression.

    Only refuses when :func:`_static_type` *knows* the type. It returns ``None``
    for anything it cannot work out statically, and guessing would reject valid
    queries; those are caught at runtime instead. Two checks rather than one,
    because neither covers the other.
    """
    static = _static_type(expression)
    if static is None or static is DataType.BOOLEAN:
        return
    raise BindingError(
        f"{clause} must be a boolean, not {static.sql_name}; "
        f"a bare value is not a condition" + (f" — {hint}" if hint else ""),
        start=expression.span.start,
        end=expression.span.end,
        line=expression.span.line,
        column=expression.span.column,
    )


def _label_of(expression: Expression) -> str:
    """A readable name for an aggregate, for EXPLAIN and error messages."""
    if isinstance(expression, FunctionCall):
        inner = "*" if expression.argument is None else _label_of(expression.argument)
        return f"{expression.function.value.lower()}({inner})"
    if isinstance(expression, BoundColumnRef):
        return expression.qualified_name
    if isinstance(expression, Literal):
        return repr(expression.value)
    return expression.node_type.lower()


#: Aggregates that need arithmetic, and so a number to do it on.
_NUMERIC_AGGREGATES: Final = frozenset({AggregateFunction.SUM, AggregateFunction.AVG})


def _require_aggregable(call: FunctionCall) -> None:
    """Refuse ``SUM`` and ``AVG`` over a type they cannot add up.

    Three bugs came out of the missing check, and each was worse than it looks:

    * ``SUM(text)`` returned ``'abd'``. Python's ``+`` concatenates strings, the
      accumulator never asked what it was adding, and the result column was
      labelled TEXT — so the engine reported a string as the sum of a column.
    * ``AVG(text)`` raised a bare ``TypeError`` from ``str / int``. Not a
      :class:`~engine.errors.ChenDBError`, so it escaped the error envelope
      entirely and would have been a 500 rather than a 400.
    * ``SUM(boolean)`` returned ``True`` over one row and ``2`` over two, still
      declaring the type BOOLEAN — a result whose Python type depended on how
      many rows were in the group, and which ``SUM(b) > 0`` then refused to
      compare because the *declared* type disagreed with the value.

    ``MIN`` and ``MAX`` are deliberately left alone. They only ever compare and
    return one of the inputs, so they are total on every type ChenDB has —
    ``MAX(name)`` and ``MAX(active)`` both mean something. PostgreSQL has no
    ``min(boolean)``, but refusing it here would be strictness for its own sake.

    ``COUNT`` takes anything, including ``*``: it counts rows, not values.
    """
    if call.function not in _NUMERIC_AGGREGATES or call.argument is None:
        return
    argument = _static_type(call.argument)
    if argument is None or argument in (DataType.INTEGER, DataType.FLOAT):
        return
    raise BindingError(
        f"{call.function.value} needs a number, not {argument.sql_name}"
        + (
            "; use COUNT to count rows or MIN/MAX to compare them"
            if argument in (DataType.TEXT, DataType.BOOLEAN)
            else ""
        ),
        start=call.span.start,
        end=call.span.end,
        line=call.span.line,
        column=call.span.column,
    )


def _plan_aggregation(
    statement: SelectStatement,
    scope: Scope,
    projections: list[Expression],
    group_keys: tuple[Expression, ...],
    having: Expression | None,
) -> BoundAggregation | None:
    """Split the select list into grouping keys and aggregates, and rewrite it.

    The grouped row is ``[key₀ … keyₖ₋₁, agg₀ … aggₘ₋₁]``. Every projection is
    rewritten in place to index into *that* row, so nothing above the aggregate
    knows an aggregate happened — a ``Project`` still reads values by position.

    Returns ``None`` when there is nothing to aggregate, which keeps every
    query written before this milestone on exactly the path it was on.
    """
    aggregates: list[BoundAggregate] = []
    seen: dict[Any, BoundAggregate] = {}

    def register(call: FunctionCall) -> BoundAggregate:
        # `SELECT COUNT(*) FROM t HAVING COUNT(*) > 1` computes it once.
        _require_aggregable(call)
        key = _signature(call)
        if key not in seen:
            slot = len(group_keys) + len(aggregates)
            entry = BoundAggregate(
                function=call.function,
                argument=call.argument,
                slot=slot,
                label=_label_of(call),
            )
            aggregates.append(entry)
            seen[key] = entry
        return seen[key]

    for expression in (*projections, *([having] if having is not None else [])):
        for node in walk(expression):
            if isinstance(node, FunctionCall):
                register(node)

    if not group_keys and not aggregates:
        return None

    keys_by_signature = {_signature(key): index for index, key in enumerate(group_keys)}
    rewritten = [
        _rewrite_over_group(expression, keys_by_signature, seen, scope)
        for expression in projections
    ]
    projections[:] = rewritten

    return BoundAggregation(
        group_keys=group_keys,
        aggregates=tuple(aggregates),
        having=(
            None
            if having is None
            else _rewrite_over_group(having, keys_by_signature, seen, scope)
        ),
    )


def _rewrite_over_group(
    expression: Expression,
    keys: dict[Any, int],
    aggregates: dict[Any, BoundAggregate],
    scope: Scope,
) -> Expression:
    """Point an expression at the grouped row instead of the input row.

    A grouping key becomes a reference to its slot; an aggregate becomes a
    reference to its slot; anything else recurses. A bare column that is
    neither is the classic error, and it is an error rather than a guess
    because SQL cannot say *which* row's value you meant — MySQL famously
    picked one and called it a feature.
    """
    signature = _signature(expression)
    if signature in keys:
        return _slot_ref(expression, keys[signature], _static_type(expression))
    if signature in aggregates:
        entry = aggregates[signature]
        return _slot_ref(
            expression,
            entry.slot,
            _aggregate_type(entry.function, entry.argument),
            name=entry.label,
        )

    match expression:
        case BoundColumnRef():
            raise BindingError(
                f"{expression.qualified_name!r} must appear in GROUP BY or be "
                f"used in an aggregate; a group is many rows and they do not "
                f"agree on it",
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
                operand=_rewrite_over_group(expression.operand, keys, aggregates, scope),
            )
        case BinaryOp():
            return BinaryOp(
                node_id=expression.node_id,
                span=expression.span,
                operator=expression.operator,
                left=_rewrite_over_group(expression.left, keys, aggregates, scope),
                right=_rewrite_over_group(expression.right, keys, aggregates, scope),
            )
        case IsNullTest():
            return IsNullTest(
                node_id=expression.node_id,
                span=expression.span,
                operand=_rewrite_over_group(expression.operand, keys, aggregates, scope),
                negated=expression.negated,
            )
    return expression


def _slot_ref(
    origin: Expression, slot: int, data_type: DataType | None, name: str = ""
) -> BoundColumnRef:
    """A reference to one column of the grouped row.

    Reusing ``BoundColumnRef`` rather than inventing a node type is the point:
    it already means "the value at index *i* of the row I was handed", and after
    an aggregate that row is the grouped one. The expression evaluator needed no
    change at all.
    """
    return BoundColumnRef(
        node_id=origin.node_id,
        span=origin.span,
        name=name or _label_of(origin),
        column_index=slot,
        data_type=data_type or DataType.INTEGER,
        table=None,
    )


def _bind_order_by(
    statement: SelectStatement,
    scope: Scope,
    projections: list[Expression],
    output_columns: list[ResultColumn],
    aggregation: BoundAggregation | None,
) -> tuple[BoundSortKey, ...]:
    """Resolve each ``ORDER BY`` term to a position in the *projected* row.

    Sorting happens after projection, so a sort key has to be something the
    projection produced. Three ways to name one, in the order they are tried:

        ORDER BY 2          the ordinal, as in the standard
        ORDER BY total      an output name or alias
        ORDER BY a.x + 1    an expression matching a projection structurally

    PostgreSQL also allows sorting by an expression over the *input* that the
    select list never mentions. That needs the sort below the projection and a
    wider intermediate row, and the error below says so rather than pretending
    the query is malformed.
    """
    if not statement.order_by:
        return ()

    by_name = {column.name: index for index, column in enumerate(output_columns)}
    by_signature = {
        _signature(projection): index for index, projection in enumerate(projections)
    }

    keys: list[BoundSortKey] = []
    for item in statement.order_by:
        index = _sort_position(item.expression, by_name, by_signature, scope, aggregation)
        if index is None:
            raise BindingError(
                "ORDER BY must name something in the SELECT list, by position, "
                "by output name, or by repeating the expression",
                start=item.span.start,
                end=item.span.end,
                line=item.span.line,
                column=item.span.column,
            )
        keys.append(
            BoundSortKey(
                output_index=index, descending=item.direction is SortDirection.DESC
            )
        )
    return tuple(keys)


def _sort_position(
    expression: Expression,
    by_name: dict[str, int],
    by_signature: dict[Any, int],
    scope: Scope,
    aggregation: BoundAggregation | None,
) -> int | None:
    if isinstance(expression, Literal) and isinstance(expression.value, int):
        # `ORDER BY 2`. One-based, as the standard says, and out of range is a
        # different error from "not in the select list".
        position = expression.value - 1
        if 0 <= position < len(by_name) + len(by_signature):
            return position
        raise BindingError(
            f"ORDER BY {expression.value} is out of range; the SELECT list has "
            f"{len(by_signature)} column(s)",
            start=expression.span.start,
            end=expression.span.end,
            line=expression.span.line,
            column=expression.span.column,
        )

    # An output alias wins over a column of the same name, which is what every
    # dialect does and why `ORDER BY total` works after `AS total`.
    if (
        isinstance(expression, ColumnRef)
        and expression.table is None
        and expression.name in by_name
    ):
        return by_name[expression.name]

    try:
        bound = bind_expression(expression, scope)
    except BindingError:
        return None
    if aggregation is not None:
        keys = {_signature(key): index for index, key in enumerate(aggregation.group_keys)}
        found = {
            _signature(
                FunctionCall(
                    node_id=0,
                    span=expression.span,
                    function=entry.function,
                    argument=entry.argument,
                )
            ): entry
            for entry in aggregation.aggregates
        }
        try:
            bound = _rewrite_over_group(bound, keys, found, scope)
        except BindingError:
            return None
    return by_signature.get(_signature(bound))


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowLayout:
    """Where one table's columns sit in the joined row, and how wide that is.

    :class:`Scope` says where a *name* resolves to; this says the same thing to
    the operator that has to build the row. It lives here rather than in the
    planner because the planner may depend on the binder and the executor may
    not depend on the planner — a scan needs this and must not import a plan.

    **A row's layout is the order the tables were written in, always.** The
    planner reorders joins freely and the layout never moves, which is what lets
    a bound column index — computed once, against the written order — stay
    correct whichever order the tables end up joined in.

    The cost is that every row below the topmost join is the full width of the
    ``FROM``, with the tables not yet joined left empty. A real engine projects
    away what it no longer needs as early as it can, and pays for that with a
    mapping from bound index to physical position at every level. This one
    trades the width for never needing the mapping.
    """

    offset: int
    width: int
    total: int

    @property
    def is_single_table(self) -> bool:
        """True for a single-table query, where there is nothing to place."""
        return self.offset == 0 and self.width == self.total

    def blank(self) -> list[Any]:
        return [None] * self.total


@dataclass(frozen=True, slots=True)
class ScopeEntry:
    """One table in a ``FROM``, and where its columns sit in the joined row."""

    binding_name: str
    """The alias if there is one, the table name otherwise. What ``a.x`` uses."""
    table_name: str
    schema: Schema
    offset: int
    """Index of this table's first column in the concatenated row."""
    position: int = 0
    """Which table in the ``FROM`` this is, counting from zero."""

    @property
    def width(self) -> int:
        return len(self.schema)


@dataclass(frozen=True, slots=True)
class Scope:
    """Every column a statement may name, and its position in the joined row.

    A join concatenates rows, so ``users`` at offset 0 with three columns and
    ``orders`` at offset 3 gives ``orders.total`` index 3 + 2 = 5. The binder
    resolves to that single flat index, and every operator above the join reads
    one list — which is why a ``Filter`` above a join needs no idea that a join
    happened.

    Ambiguity is resolved here rather than at runtime. ``id`` in a two-table
    join where both tables have one is an error, not a coin flip, and the error
    names both candidates.
    """

    entries: tuple[ScopeEntry, ...]

    @property
    def width(self) -> int:
        return sum(entry.width for entry in self.entries)

    @property
    def is_single_table(self) -> bool:
        return len(self.entries) == 1

    def columns(self) -> tuple[tuple[ScopeEntry, int, Column], ...]:
        """Every column, with its table and its index in the joined row."""
        return tuple(
            (entry, entry.offset + position, column)
            for entry in self.entries
            for position, column in enumerate(entry.schema)
        )

    def entry(self, binding_name: str) -> ScopeEntry | None:
        folded = binding_name.casefold()
        for entry in self.entries:
            if entry.binding_name.casefold() == folded:
                return entry
        return None

    @classmethod
    def of(cls, schema: Schema, table_name: str = "") -> Scope:
        """A one-table scope, for the callers that never had more than one."""
        return cls((ScopeEntry(table_name, table_name, schema, 0, 0),))

    def owner_of(self, index: int) -> ScopeEntry:
        """Which table a joined-row index belongs to."""
        for entry in self.entries:
            if entry.offset <= index < entry.offset + entry.width:
                return entry
        raise IndexError(index)  # pragma: no cover - indices come from here


def build_scope(
    tables: Sequence[TableRef], catalog: CatalogLike, statement: Statement
) -> Scope:
    """Resolve every table in a ``FROM`` and lay their columns out end to end."""
    entries: list[ScopeEntry] = []
    offset = 0
    for reference in tables:
        info = _resolve_table(reference.name, catalog, statement, span=reference.span)
        binding = reference.binding_name
        if any(entry.binding_name == binding for entry in entries):
            raise BindingError(
                f"{binding!r} is used twice in FROM; give one of them an alias",
                start=reference.span.start,
                end=reference.span.end,
                line=reference.span.line,
                column=reference.span.column,
            )
        entries.append(ScopeEntry(binding, info.name, info.schema, offset, len(entries)))
        offset += len(info.schema)
    return Scope(tuple(entries))


def bind_select(statement: SelectStatement, catalog: CatalogLike) -> BoundSelect:
    """Bind a ``SELECT`` against the catalog.

    Four passes, in the order SQL evaluates them, which is not the order it is
    written in: ``FROM`` builds the scope, ``WHERE`` and the join conditions
    bind against it, ``GROUP BY`` fixes what a row means from there on, and only
    then can the select list and ``HAVING`` be bound — because after grouping,
    a bare column reference is an error unless it is a grouping key.
    """
    scope = build_scope(statement.tables, catalog, statement)

    joins: list[BoundJoin] = []
    for clause in statement.joins:
        entry = scope.entry(clause.table.binding_name)
        assert entry is not None
        condition = bind_expression(clause.on, scope)
        _reject_aggregates(condition, "a JOIN condition")
        _require_predicate(condition, "a JOIN condition")
        joins.append(
            BoundJoin(
                binding_name=entry.binding_name,
                condition=condition,
                kind=clause.kind,
            )
        )

    where = bind_expression(statement.where, scope) if statement.where else None
    if where is not None:
        # WHERE runs before grouping, so it cannot see an aggregate. PostgreSQL
        # says "aggregate functions are not allowed in WHERE"; the fix is
        # HAVING, and saying so is more use than the rule.
        _reject_aggregates(
            where, "WHERE", hint="use HAVING for a condition on an aggregate"
        )
        _require_predicate(where, "WHERE", hint="did you mean a comparison?")

    group_keys = tuple(bind_expression(key, scope) for key in statement.group_by)
    for key in group_keys:
        _reject_aggregates(key, "GROUP BY")

    projections: list[Expression] = []
    output_columns: list[ResultColumn] = []
    for item in statement.projections:
        for expression, name in _expand_item(item, scope):
            projections.append(expression)
            output_columns.append(ResultColumn(name, _static_type(expression)))

    having = bind_expression(statement.having, scope) if statement.having else None
    if having is not None:
        _require_predicate(having, "HAVING")
    if having is not None and not group_keys and not _any_aggregate(having):
        raise BindingError(
            "HAVING without GROUP BY needs an aggregate; a plain condition "
            "belongs in WHERE",
            start=statement.having.span.start,
            end=statement.having.span.end,
            line=statement.having.span.line,
            column=statement.having.span.column,
        )

    aggregation = _plan_aggregation(statement, scope, projections, group_keys, having)
    order_by = _bind_order_by(statement, scope, projections, output_columns, aggregation)

    return BoundSelect(
        table_name=scope.entries[0].table_name,
        input_schema=scope.entries[0].schema,
        scope=scope,
        joins=tuple(joins),
        projections=tuple(projections),
        output_columns=tuple(output_columns),
        where=where,
        aggregation=aggregation,
        order_by=order_by,
        limit=statement.limit,
        offset=statement.offset,
        statement=statement,
    )


def _expand_item(item: SelectItem, scope: Scope) -> list[tuple[Expression, str]]:
    """One select-list entry, as one or more bound expressions with names.

    ``*`` and ``u.*`` are expanded here so the executor never sees a ``Star``
    and the output width is fixed before a single row is read.
    """
    if isinstance(item.expression, Star):
        if item.alias is not None:
            raise BindingError(
                "'*' cannot be aliased",
                start=item.span.start,
                end=item.span.end,
                line=item.span.line,
                column=item.span.column,
            )
        wanted = item.expression.table
        if wanted is not None and scope.entry(wanted) is None:
            raise BindingError(
                f"no table named {wanted!r} in FROM; this query has "
                f"{', '.join(entry.binding_name for entry in scope.entries)}",
                start=item.span.start,
                end=item.span.end,
                line=item.span.line,
                column=item.span.column,
            )
        out: list[tuple[Expression, str]] = []
        for entry, index, column in scope.columns():
            if wanted is not None and entry.binding_name != wanted:
                continue
            out.append(
                (
                    BoundColumnRef(
                        node_id=item.expression.node_id,
                        span=item.expression.span,
                        name=column.name,
                        column_index=index,
                        data_type=column.data_type,
                        table=entry.binding_name,
                        scan_position=entry.position,
                        table_name=entry.table_name,
                        table_position=index - entry.offset,
                    ),
                    # A join can put two `id` columns side by side. Qualifying
                    # them keeps the header readable and honest; PostgreSQL
                    # emits two columns both called `id`, which is worse.
                    column.name
                    if scope.is_single_table
                    else f"{entry.binding_name}.{column.name}",
                )
            )
        return out

    bound = bind_expression(item.expression, scope)
    return [(bound, _output_name(item, bound))]


def _output_name(item: SelectItem, bound: Expression) -> str:
    if item.alias:
        return item.alias
    if isinstance(bound, BoundColumnRef):
        return bound.name
    if isinstance(bound, FunctionCall):
        # `count(*)`, as PostgreSQL names it. Not "?column?": an aggregate has
        # a perfectly good name and hiding it makes the header useless.
        return _label_of(bound)
    # PostgreSQL calls an unnamed expression "?column?". Being explicit that the
    # name is synthesised is better than inventing something that looks real.
    return "?column?"


def bind_insert(statement: InsertStatement, catalog: CatalogLike) -> BoundInsert:
    """Bind an ``INSERT``, reordering values into schema order.

    A statement may name its columns in any order and omit nullable ones. The
    executor should not have to care, so every row comes out of here full-width
    and in declaration order, with ``NULL`` literals filled in for omissions.
    """
    info = _resolve_writable_table(statement.table.name, catalog, statement)
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


def identity_projection(
    schema: Schema, node: Statement
) -> tuple[tuple[Expression, ...], tuple[ResultColumn, ...]]:
    """Every column of ``schema``, in order — what ``SELECT *`` expands to.

    ``UPDATE`` and ``DELETE`` need the whole row even though they return none of
    it: the predicate can name any column, an index entry is keyed on the value
    it holds, and ``SET n = n + 1`` reads the version it is replacing. Sharing
    the expansion with ``SELECT *`` is what lets the same planner cost all three.
    """
    projections = tuple(
        BoundColumnRef(
            node_id=node.node_id,
            span=node.span,
            name=column.name,
            column_index=index,
            data_type=column.data_type,
        )
        for index, column in enumerate(schema)
    )
    outputs = tuple(ResultColumn(column.name, column.data_type) for column in schema)
    return projections, outputs


def _writable_scope(info: Any) -> Scope:
    """A one-table scope that knows the table's *name*.

    ``UPDATE`` and ``DELETE`` used to bind against a bare :class:`Schema`, which
    :func:`bind_expression` wraps in a scope whose binding name is the empty
    string. So the table had no name to qualify with and
    ``DELETE FROM child WHERE child.c_int = 2`` was refused — with
    ``no table named 'child' in FROM; this query has `` and nothing after the
    ``has``, because the one entry it could have listed was nameless.

    Standard SQL allows the target table to be qualified, and both SQLite and
    PostgreSQL accept it. Naming the entry fixes the refusal and the message
    together.
    """
    return Scope.of(info.schema, info.name)


def bind_delete(statement: DeleteStatement, catalog: CatalogLike) -> BoundDelete:
    """Bind a ``DELETE``. Only the table and the predicate need resolving."""
    info = _resolve_writable_table(statement.table.name, catalog, statement)
    where = (
        bind_expression(statement.where, _writable_scope(info)) if statement.where else None
    )
    return BoundDelete(
        table_name=info.name,
        schema=info.schema,
        where=where,
        statement=statement,
    )


def bind_update(statement: UpdateStatement, catalog: CatalogLike) -> BoundUpdate:
    """Bind an ``UPDATE``: resolve each target, bind each value and the predicate.

    Assignments keep the order they were written in, but that order is *not*
    semantic. ``SET a = b, b = a`` swaps the two columns, because both right-hand
    sides are evaluated against the row as it was before any of them ran. Every
    SQL engine does this, and it is the one thing about ``SET`` that surprises
    people coming from a procedural language.
    """
    info = _resolve_writable_table(statement.table.name, catalog, statement)
    schema = info.schema
    scope = _writable_scope(info)

    assignments: list[BoundAssignment] = []
    seen: dict[int, str] = {}
    for assignment in statement.assignments:
        try:
            index = schema.index_of(assignment.column)
        except SchemaError:
            raise BindingError(
                f"no column named {assignment.column!r}; "
                f"this table has {', '.join(schema.column_names)}",
                start=assignment.span.start,
                end=assignment.span.end,
                line=assignment.span.line,
                column=assignment.span.column,
            ) from None

        if index in seen:
            raise BindingError(
                f"column {schema[index].name!r} is assigned twice",
                start=assignment.span.start,
                end=assignment.span.end,
                line=assignment.span.line,
                column=assignment.span.column,
            )
        seen[index] = assignment.column

        value = bind_expression(assignment.value, scope)
        _check_assignable(assignment, schema[index], value)
        assignments.append(BoundAssignment(index, schema[index], value))

    where = bind_expression(statement.where, scope) if statement.where else None
    return BoundUpdate(
        table_name=info.name,
        schema=schema,
        assignments=tuple(assignments),
        where=where,
        statement=statement,
    )


def _check_assignable(assignment: Assignment, column: Column, value: Expression) -> None:
    """Reject what is statically known to be wrong, and only that.

    The encoder catches the rest at write time, but it has no source position and
    fires half-way through a statement. Anything whose type is not known until a
    row is in hand — ``SET a = b`` between two columns, arithmetic over mixed
    types — is left to it rather than guessed at here.
    """
    if isinstance(value, Literal) and value.value is None and not column.nullable:
        raise BindingError(
            f"column {column.name!r} is NOT NULL",
            start=assignment.span.start,
            end=assignment.span.end,
            line=assignment.span.line,
            column=assignment.span.column,
        )

    static = _static_type(value)
    if static is None or static is column.data_type:
        return
    # INTEGER into FLOAT widens without loss and every dialect allows it. The
    # reverse does not, so it is rejected rather than silently truncated.
    if column.data_type is DataType.FLOAT and static is DataType.INTEGER:
        return
    raise BindingError(
        f"cannot assign {static.sql_name} to column {column.name!r}, "
        f"which is {column.data_type.sql_name}",
        start=assignment.span.start,
        end=assignment.span.end,
        line=assignment.span.line,
        column=assignment.span.column,
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


#: Statements that name a table in a ``table`` field, so an error about the name
#: can point at the name rather than at the whole statement.
_TABLE_BEARING = SelectStatement | InsertStatement | UpdateStatement | DeleteStatement


def _resolve_table(
    referenced: str,
    catalog: CatalogLike,
    statement: Statement,
    *,
    span: SourceSpan | None = None,
) -> TableInfo:
    """Look up a table, turning a miss into a positioned :class:`BindingError`.

    The message lists what *does* exist, because "no such table" without that is
    the least helpful error a database can give. ``span`` overrides the position
    for a ``FROM`` with several tables in it, where the statement's own span
    would underline all of them.
    """
    info = catalog.get_table(referenced)
    if info is not None:
        return info

    known = ", ".join(table.name for table in catalog.list_tables())
    detail = f"this database has {known}" if known else "this database has no tables"
    if span is None:
        span = (
            statement.table.span
            if isinstance(statement, _TABLE_BEARING)
            else statement.span
        )
    raise BindingError(
        f"no table named {referenced!r}; {detail}",
        start=span.start,
        end=span.end,
        line=span.line,
        column=span.column,
    )


def _resolve_writable_table(
    referenced: str, catalog: CatalogLike, statement: Statement
) -> TableInfo:
    """:func:`_resolve_table`, refusing the catalog's own tables.

    ``chendb_tables`` and ``chendb_columns`` are readable — that is how the UI
    shows a schema — but writing to them through SQL would let a ``DELETE`` drop
    a table's definition out from under the heap that still holds its rows. DDL
    is the only supported way to change them, and it goes through
    :class:`~engine.catalog.catalog.Catalog`, which keeps both sides in step.

    PostgreSQL's rule is the same in spirit and stricter in practice: even a
    superuser is refused ``DELETE FROM pg_class`` unless
    ``allow_system_table_mods`` is on.
    """
    info = _resolve_table(referenced, catalog, statement)
    if not info.is_system:
        return info
    span = statement.table.span if isinstance(statement, _TABLE_BEARING) else statement.span
    raise BindingError(
        f"{info.name!r} is a system table; it can be read but not written to",
        start=span.start,
        end=span.end,
        line=span.line,
        column=span.column,
    )
