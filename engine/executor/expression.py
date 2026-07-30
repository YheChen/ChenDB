"""Expression evaluation, with SQL's three-valued logic.

An expression is evaluated against one row at a time.  Column references have
already been resolved to positional indices by the binder, so evaluation is a
tree walk with no name lookups.

Three-valued logic
------------------
SQL is not boolean logic.  ``NULL`` means *unknown*, and comparing against an
unknown gives an unknown — never true, never false:

    NULL = NULL   →  NULL        (not TRUE)
    NULL = 1      →  NULL        (not FALSE)
    NULL <> 1     →  NULL
    1 + NULL      →  NULL

``AND`` and ``OR`` are where it gets interesting, because they can *short-circuit
through* an unknown:

    ┌──────────────────┬──────────────────┐
    │ AND │ T │ F │ N  │  OR │ T │ F │ N  │
    ├─────┼───┼───┼────┼─────┼───┼───┼────┤
    │  T  │ T │ F │ N  │  T  │ T │ T │ T  │
    │  F  │ F │ F │ F  │  F  │ T │ F │ N  │
    │  N  │ N │ F │ N  │  N  │ T │ N │ N  │
    └──────────────────┴──────────────────┘

``FALSE AND NULL`` is ``FALSE``, because whatever the unknown turns out to be,
the conjunction cannot be true.  Symmetrically ``TRUE OR NULL`` is ``TRUE``.
Getting this wrong is the single most common source of wrong answers in a
hand-written query engine.

And the payoff, in :class:`~engine.executor.operators.Filter`:

    a row passes ``WHERE`` only when the predicate is **exactly TRUE**.
    NULL is not TRUE, so `WHERE age > 18` silently drops rows with a NULL age.

That is why ``SELECT count(*) FROM t`` and
``SELECT count(*) FROM t WHERE x = x`` can disagree, and it is required
behaviour, not a bug.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Sequence
from fractions import Fraction
from typing import Any, Final

from engine.diagnostics.events import ExpressionEvalEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import EvaluationError
from engine.executor.binder import BoundColumnRef
from engine.parser.ast import (
    BinaryOp,
    BinaryOperator,
    Expression,
    IsNullTest,
    Literal,
    Star,
    UnaryOp,
    UnaryOperator,
)
from engine.serialization.types import INT64_MAX, INT64_MIN, DataType

__all__ = ["check_numeric_range", "describe_expression", "evaluate", "is_true"]

#: Comparison operators. Applied only after both operands are known non-NULL.
_COMPARISONS: Final[dict[BinaryOperator, Callable[[Any, Any], bool]]] = {
    BinaryOperator.EQ: operator.eq,
    BinaryOperator.NEQ: operator.ne,
    BinaryOperator.LT: operator.lt,
    BinaryOperator.LTE: operator.le,
    BinaryOperator.GT: operator.gt,
    BinaryOperator.GTE: operator.ge,
}

_ARITHMETIC: Final[dict[BinaryOperator, Callable[[Any, Any], Any]]] = {
    BinaryOperator.ADD: operator.add,
    BinaryOperator.SUBTRACT: operator.sub,
    BinaryOperator.MULTIPLY: operator.mul,
}

#: Above this an integer is not exactly a double, so mixed int/float
#: arithmetic has to be done another way. 2**53.
_EXACT_IN_DOUBLE: Final = 9007199254740992

#: Types that can be compared with each other. TEXT is not comparable to a
#: number: SQL requires an explicit cast, and silently coercing "10" < 9 to
#: either answer would be worse than refusing.
_NUMERIC: Final = frozenset({DataType.INTEGER, DataType.FLOAT})


def evaluate(
    expression: Expression,
    row: Sequence[Any],
    *,
    tracer: Tracer | None = None,
    operator_id: str = "",
) -> Any:
    """Evaluate ``expression`` against ``row``. Returns ``None`` for SQL NULL.

    ``row`` is positional; column references must already be bound to indices.
    """
    tracer = tracer if tracer is not None else NULL_TRACER
    value = _evaluate(expression, row, tracer, operator_id)
    if tracer.verbose:
        tracer.emit(
            ExpressionEvalEvent(
                operator_id=operator_id,
                node_id=expression.node_id,
                expression=describe_expression(expression),
                result=_render(value),
            )
        )
    return value


def _evaluate(
    expression: Expression, row: Sequence[Any], tracer: Tracer, operator_id: str
) -> Any:
    match expression:
        case Literal():
            return expression.value

        case BoundColumnRef():
            try:
                return row[expression.column_index]
            except IndexError:  # pragma: no cover - binder guarantees the width
                raise EvaluationError(
                    f"column index {expression.column_index} is outside a "
                    f"{len(row)}-column row"
                ) from None

        case IsNullTest():
            # The one construct that can *see* a NULL rather than propagating
            # it. It always returns TRUE or FALSE, never NULL.
            value = _evaluate(expression.operand, row, tracer, operator_id)
            return (value is not None) if expression.negated else (value is None)

        case UnaryOp():
            return _evaluate_unary(expression, row, tracer, operator_id)

        case BinaryOp():
            return _evaluate_binary(expression, row, tracer, operator_id)

        case Star():
            raise EvaluationError(
                "'*' is expanded by the binder and cannot be evaluated directly"
            )

    raise EvaluationError(f"cannot evaluate {expression.node_type}")


def _evaluate_unary(
    expression: UnaryOp, row: Sequence[Any], tracer: Tracer, operator_id: str
) -> Any:
    value = _evaluate(expression.operand, row, tracer, operator_id)

    match expression.operator:
        case UnaryOperator.NOT:
            # NOT NULL is NULL, not TRUE. The unknown stays unknown.
            if value is None:
                return None
            _require_boolean(value, "NOT")
            return not value
        case UnaryOperator.NEGATE:
            if value is None:
                return None
            _require_number(value, "unary -")
            # Negation overflows in exactly one place, and it is the one people
            # forget: int64 is asymmetric, so -(-9223372036854775808) has no
            # int64 to be.
            return check_numeric_range(-value, "unary -")
        case UnaryOperator.PLUS:
            if value is None:
                return None
            _require_number(value, "unary +")
            return value

    raise EvaluationError(f"unknown unary operator {expression.operator}")


def _evaluate_binary(
    expression: BinaryOp, row: Sequence[Any], tracer: Tracer, operator_id: str
) -> Any:
    op = expression.operator

    # AND and OR are evaluated lazily: their truth tables let a known operand
    # decide the result even when the other side is NULL, and short-circuiting
    # also avoids evaluating a subtree that cannot change the answer.
    if op is BinaryOperator.AND:
        return _evaluate_and(expression, row, tracer, operator_id)
    if op is BinaryOperator.OR:
        return _evaluate_or(expression, row, tracer, operator_id)

    left = _evaluate(expression.left, row, tracer, operator_id)
    right = _evaluate(expression.right, row, tracer, operator_id)

    # Every remaining operator propagates NULL: an unknown input means an
    # unknown result.
    if left is None or right is None:
        return None

    if op in _COMPARISONS:
        _require_comparable(left, right, op)
        return _COMPARISONS[op](left, right)

    if op in _ARITHMETIC:
        _require_number(left, op.value)
        _require_number(right, op.value)
        return check_numeric_range(_ARITHMETIC[op](left, right), op.value)

    if op is BinaryOperator.DIVIDE:
        return _divide(left, right)
    if op is BinaryOperator.MODULO:
        return _modulo(left, right)

    raise EvaluationError(f"unknown binary operator {op}")


def _evaluate_and(
    expression: BinaryOp, row: Sequence[Any], tracer: Tracer, operator_id: str
) -> bool | None:
    left = _evaluate(expression.left, row, tracer, operator_id)
    if left is not None:
        _require_boolean(left, "AND")
        if left is False:
            return False  # FALSE AND anything is FALSE, even NULL

    right = _evaluate(expression.right, row, tracer, operator_id)
    if right is not None:
        _require_boolean(right, "AND")
        if right is False:
            return False

    if left is None or right is None:
        return None  # TRUE AND NULL, or NULL AND NULL
    return True


def _evaluate_or(
    expression: BinaryOp, row: Sequence[Any], tracer: Tracer, operator_id: str
) -> bool | None:
    left = _evaluate(expression.left, row, tracer, operator_id)
    if left is not None:
        _require_boolean(left, "OR")
        if left is True:
            return True  # TRUE OR anything is TRUE, even NULL

    right = _evaluate(expression.right, row, tracer, operator_id)
    if right is not None:
        _require_boolean(right, "OR")
        if right is True:
            return True

    if left is None or right is None:
        return None
    return False


def _divide(left: Any, right: Any) -> Any:
    _require_number(left, "/")
    _require_number(right, "/")
    if right == 0:
        # SQL raises rather than returning NULL or infinity. PostgreSQL and
        # SQLite differ here — SQLite returns NULL — and raising is the choice
        # that never silently hides a bug in the query.
        raise EvaluationError("division by zero")
    if isinstance(left, int) and isinstance(right, int):
        # Integer division truncates toward zero, as in C and PostgreSQL, not
        # toward negative infinity as Python's // does.
        quotient = abs(left) // abs(right)
        return -quotient if (left < 0) != (right < 0) else quotient
    return check_numeric_range(left / right, "/")


def _modulo(left: Any, right: Any) -> Any:
    _require_number(left, "%")
    _require_number(right, "%")
    if right == 0:
        raise EvaluationError("modulo by zero")
    # Sign follows the dividend, matching C and PostgreSQL rather than Python.
    magnitude = _exact_remainder(abs(left), abs(right))
    return check_numeric_range(-magnitude if left < 0 else magnitude, "%")


def _exact_remainder(left: Any, right: Any) -> Any:
    """``left % right`` for non-negative operands, without a silent widening.

    Python's ``int % float`` converts the integer to a double first, and an int64
    need not survive that: ``9223372036854775807 % 2.0`` rounded the dividend up
    to 2⁶³ and answered ``0.0`` for an odd number. Both SQLite and PostgreSQL say
    ``1``.

    :class:`~fractions.Fraction` is exact over both, because every finite double
    *is* a rational. It is only reached when an integer is too large to be a
    double exactly — outside 2⁵³ — so the ordinary path keeps doing one machine
    modulo, and the expensive one runs where the cheap one would be wrong.
    """
    mixed = isinstance(left, int) != isinstance(right, int)
    if mixed and max(abs(left), abs(right)) > _EXACT_IN_DOUBLE:
        return float(Fraction(left) % Fraction(right))
    return left % right


def check_numeric_range(value: Any, what: str) -> Any:
    """Return ``value``, or raise if it is a number the engine cannot represent.

    An expression must not be able to produce a value that a column of the same
    type would refuse. It could, in both directions, and both were found by
    asking SQLite the same questions:

    ``INTEGER`` is 64 bits — :mod:`engine.serialization.types` says so and the
    codec enforces it on the way to disk — but nothing enforced it in an
    expression, so ``SELECT n + 1`` on the largest int64 returned
    9223372036854775808 under a column labelled INTEGER. Python's unbounded
    integers are both why it was possible and why the check is needed; in C it
    would have wrapped, which is worse.

    ``FLOAT`` is a *finite* double, for the reasons :class:`FloatCodec` sets out
    at length, so ``1e308 * 10`` must not quietly hand back ``inf`` either.

    PostgreSQL raises ``integer out of range`` for the first and keeps infinity
    for the second; SQLite promotes the integer to a float and turns the
    infinity into NULL on store. Both of those change the *type* of an answer to
    avoid an error. Raising keeps each type meaning exactly one thing, wherever
    the value came from.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not INT64_MIN <= value <= INT64_MAX:
        raise EvaluationError(
            f"{what} overflowed: {value} does not fit in a 64-bit INTEGER "
            f"[{INT64_MIN}, {INT64_MAX}]"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise EvaluationError(
            f"{what} produced {value}, which FLOAT cannot represent; NaN and "
            f"infinity have no total order, so nothing could sort or index it"
        )
    return value


# -- type checks -----------------------------------------------------------


def _require_boolean(value: Any, what: str) -> None:
    if not isinstance(value, bool):
        raise EvaluationError(f"{what} needs a boolean operand, got {_type_name(value)}")


def _require_number(value: Any, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(f"{what} needs a numeric operand, got {_type_name(value)}")


def _require_comparable(left: Any, right: Any, op: BinaryOperator) -> None:
    """Reject comparisons SQL would require a cast for.

    Numbers compare with numbers, booleans with booleans, text with text.
    Python would happily order ``"10" < "9"`` lexicographically or refuse
    ``1 < "a"`` with a TypeError; neither is a good error message.
    """
    left_kind = _kind(left)
    right_kind = _kind(right)
    if left_kind != right_kind:
        raise EvaluationError(
            f"cannot compare {left_kind} with {right_kind} using {op.value}; "
            f"SQL requires an explicit cast"
        )


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "TEXT"
    return type(value).__name__


def _type_name(value: Any) -> str:
    return "NULL" if value is None else _kind(value)


def _render(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return repr(value)
    return str(value)


# -- helpers ---------------------------------------------------------------


def is_true(value: Any, *, clause: str = "a predicate") -> bool:
    """Whether a predicate result lets a row through a ``WHERE``.

    Only an exact ``True`` passes. ``None`` (unknown) and ``False`` both filter
    the row out — the reason `WHERE age > 18` drops rows whose age is NULL.

    A value that is not a boolean at all is an **error**, not a rejection. That
    distinction was worth a bug: ``WHERE v`` over an INTEGER column used to
    return no rows and no complaint, because ``5 is True`` is ``False`` and a
    filter cannot tell "this row failed" from "this was never a condition". The
    same silence dropped every group from ``HAVING SUM(v)``. Differential
    testing against SQLite is what found it, after thirteen milestones —
    precisely the shape of bug a hand-written test suite does not look for,
    because you have to think of the query first.

    ``AND`` and ``OR`` have always checked their operands (:func:`_require_boolean`).
    This closes the one path that did not: a predicate that is a bare column or
    a bare arithmetic expression, with no logical operator anywhere in it.
    """
    if value is None or isinstance(value, bool):
        return value is True
    raise EvaluationError(
        f"{clause} must be a boolean, got {_type_name(value)}; "
        f"a bare value is not a condition"
    )


def describe_expression(expression: Expression) -> str:
    """Render an expression back to SQL-ish text, for events and plan display."""
    match expression:
        case Literal():
            return _render(expression.value)
        case BoundColumnRef():
            return expression.name
        case Star():
            return "*"
        case IsNullTest():
            suffix = "IS NOT NULL" if expression.negated else "IS NULL"
            return f"{describe_expression(expression.operand)} {suffix}"
        case UnaryOp():
            inner = describe_expression(expression.operand)
            if expression.operator is UnaryOperator.NOT:
                return f"NOT {inner}"
            return f"{expression.operator.value}{inner}"
        case BinaryOp():
            left = describe_expression(expression.left)
            right = describe_expression(expression.right)
            return f"({left} {expression.operator.value} {right})"
    return expression.node_type
