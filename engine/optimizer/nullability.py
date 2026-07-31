"""Proving that a predicate cannot be TRUE once a table's columns are all NULL.

This is the one analysis an optimiser needs before it may touch an outer join,
and it is worth stating precisely, because the whole rewrite in
:func:`~engine.optimizer.rules._simplify_outer_joins` rests on it.

An outer join preserves rows that found no partner by extending them with NULLs.
If a filter above the join can never say TRUE about such a row, every preserved
row is thrown away a moment later, and preserving them was never observable. The
join was an inner join written the long way.

    a LEFT JOIN b ON a.id = b.id WHERE b.x = 5

``b.x`` is NULL in every preserved row, ``NULL = 5`` is NULL, and a WHERE keeps
only TRUE. So the preserved rows cannot survive and the LEFT is an INNER.

How the proof works
-------------------
Not by evaluating the predicate, which would need a row, but by evaluating it
over *sets of possible outcomes*. Every column of a NULL-supplied table is known
to be NULL; every other value is unknown and could be anything. Push those two
facts through SQL's own truth tables and read off the set of results the
predicate could produce. If TRUE is not in it, the predicate rejects NULLs.

This is abstract interpretation, and it is sound in the direction that matters
because every rule below returns a **superset** of what can really happen. A
superset that omits TRUE is a proof that TRUE is impossible. The cost of being
conservative is a missed rewrite, never a wrong answer, which is the correct way
round: :func:`_truth` returns :data:`ANY` for anything it does not recognise, so
a new expression node is safe by default rather than dangerous by default.

Why not "is this operator strict?"
----------------------------------
PostgreSQL asks a related question through ``clause_is_strict_for`` and its
``find_nonnullable_rels`` walk, which reasons about strict *functions*: one that
returns NULL whenever any argument is NULL. That is the right shape for an engine
whose operators are catalogue entries with a strictness flag on them. ChenDB has
a fixed handful of operators and no catalogue of functions, so the truth tables
can be written out directly, and writing them out buys two things a strictness
flag does not: ``IS NULL`` (not strict, and the one predicate that goes *TRUE*
on a NULL, which is exactly the anti-join idiom that must not be rewritten) and
``OR`` (not strict either, since ``NULL OR TRUE`` is TRUE).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Flag, auto
from typing import Final

from engine.executor.binder import BoundColumnRef
from engine.parser.ast import (
    BinaryOp,
    BinaryOperator,
    Expression,
    IsNullTest,
    Literal,
    UnaryOp,
    UnaryOperator,
)

__all__ = ["ANY", "Truth", "rejects_nulls", "truth_of"]


class Truth(Flag):
    """Which of SQL's three results an expression could produce.

    A *set*, not a value, which is the whole point. ``Truth.FALSE |
    Truth.UNKNOWN`` says "this is definitely not TRUE, and I cannot say which of
    the other two it is", and that is precisely the fact the rewrite needs.
    """

    TRUE = auto()
    FALSE = auto()
    UNKNOWN = auto()


#: No information: the expression could come out any of the three ways.
ANY: Final = Truth.TRUE | Truth.FALSE | Truth.UNKNOWN

#: NULL, spelled as the truth value SQL gives it in a condition.
_NULL: Final = Truth.UNKNOWN

_AND_TABLE: Final[dict[tuple[Truth, Truth], Truth]] = {
    (Truth.TRUE, Truth.TRUE): Truth.TRUE,
    (Truth.TRUE, Truth.FALSE): Truth.FALSE,
    (Truth.TRUE, Truth.UNKNOWN): Truth.UNKNOWN,
    (Truth.FALSE, Truth.TRUE): Truth.FALSE,
    (Truth.FALSE, Truth.FALSE): Truth.FALSE,
    # FALSE AND unknown is FALSE. It cannot be true whatever the unknown was.
    (Truth.FALSE, Truth.UNKNOWN): Truth.FALSE,
    (Truth.UNKNOWN, Truth.TRUE): Truth.UNKNOWN,
    (Truth.UNKNOWN, Truth.FALSE): Truth.FALSE,
    (Truth.UNKNOWN, Truth.UNKNOWN): Truth.UNKNOWN,
}

_OR_TABLE: Final[dict[tuple[Truth, Truth], Truth]] = {
    (Truth.TRUE, Truth.TRUE): Truth.TRUE,
    (Truth.TRUE, Truth.FALSE): Truth.TRUE,
    # TRUE OR unknown is TRUE, which is why OR only rejects NULLs when *both*
    # sides do. One survivable branch is enough to keep a preserved row alive.
    (Truth.TRUE, Truth.UNKNOWN): Truth.TRUE,
    (Truth.FALSE, Truth.TRUE): Truth.TRUE,
    (Truth.FALSE, Truth.FALSE): Truth.FALSE,
    (Truth.FALSE, Truth.UNKNOWN): Truth.UNKNOWN,
    (Truth.UNKNOWN, Truth.TRUE): Truth.TRUE,
    (Truth.UNKNOWN, Truth.FALSE): Truth.UNKNOWN,
    (Truth.UNKNOWN, Truth.UNKNOWN): Truth.UNKNOWN,
}

_NOT_TABLE: Final[dict[Truth, Truth]] = {
    Truth.TRUE: Truth.FALSE,
    Truth.FALSE: Truth.TRUE,
    Truth.UNKNOWN: Truth.UNKNOWN,
}


def rejects_nulls(predicates: Iterable[Expression], sources: frozenset[int]) -> bool:
    """Whether the conjunction of ``predicates`` can never be TRUE.

    ``sources`` are scan positions whose every column is NULL, which is what an
    outer join does to the side that found no partner.

    The predicates are treated as one ``AND`` because that is how a WHERE and the
    ``ON`` clauses above a join combine: a row has to satisfy all of them.

    Taking them together turns out to be exactly as strong as taking them one at
    a time, and it is worth knowing why rather than assuming otherwise. ``AND``
    is applied pointwise over two sets of outcomes, and ``TRUE AND TRUE`` is
    ``TRUE``, so TRUE survives the combination precisely when it was in both
    sides to begin with. The analysis carries no correlation *between* separate
    predicates and loses nothing by it. Inside a single expression it does
    better, because there each side is one known outcome rather than a set:
    ``b.x IS NULL AND b.x IS NOT NULL`` is proved FALSE.

    An empty sequence returns ``False``. Nothing is filtering, so nothing is
    proved, and that is the honest answer rather than a vacuous truth.
    """
    outcome = Truth.TRUE
    for predicate in predicates:
        outcome = _combine(outcome, truth_of(predicate, sources), _AND_TABLE)
        if outcome == Truth.FALSE:
            break  # FALSE AND anything is FALSE, so no later term can change it
    return Truth.TRUE not in outcome


def truth_of(expression: Expression, sources: frozenset[int]) -> Truth:
    """Every result ``expression`` could produce with ``sources`` all NULL.

    Always a superset of the truth. See the module docstring for why that
    direction is the safe one.
    """
    match expression:
        # Spelled as a type test rather than as `value=True`, which would be a
        # literal pattern and so depend on knowing that `match` compares None,
        # True and False by identity while comparing everything else with `==`.
        # Under equality `Literal(1)` would match `value=True`.
        case Literal(value=bool() as value):
            return Truth.TRUE if value else Truth.FALSE
        case Literal(value=None):
            return _NULL
        case Literal():
            # A non-boolean literal is not a condition at all; ChenDB raises on
            # one since Milestone 17. Reporting ANY declines to rewrite rather
            # than reasoning about a query that will not run.
            return ANY

        case IsNullTest(operand=operand, negated=negated):
            # The only expression that can *see* a NULL, and so the only one that
            # goes TRUE on the rows an outer join invented. It never returns
            # UNKNOWN, whatever its operand does.
            if _definitely_null(operand, sources):
                return Truth.FALSE if negated else Truth.TRUE
            return Truth.TRUE | Truth.FALSE

        case UnaryOp(operator=UnaryOperator.NOT, operand=operand):
            return _map(truth_of(operand, sources), _NOT_TABLE)

        case BinaryOp(operator=BinaryOperator.AND, left=left, right=right):
            return _combine(truth_of(left, sources), truth_of(right, sources), _AND_TABLE)

        case BinaryOp(operator=BinaryOperator.OR, left=left, right=right):
            return _combine(truth_of(left, sources), truth_of(right, sources), _OR_TABLE)

        case BinaryOp(operator=operator, left=left, right=right) if operator.is_comparison:
            # Every comparison is strict: compare against an unknown and the
            # answer is unknown. This one line is what makes `b.x = 5`,
            # `b.x <> 5` and `a.n < b.x` all reject NULLs.
            if _definitely_null(left, sources) or _definitely_null(right, sources):
                return _NULL
            return ANY

        case BoundColumnRef(scan_position=position):
            # A BOOLEAN column standing alone is a condition.
            return _NULL if position in sources else ANY

    return ANY


def _definitely_null(expression: Expression, sources: frozenset[int]) -> bool:
    """Whether ``expression`` is NULL for certain, not merely possibly.

    The asymmetry is deliberate. Saying "null" wrongly would let the rewrite
    fire on a predicate that can still be TRUE; saying "not null" wrongly only
    costs a rewrite. So every case that is not proved returns ``False``.
    """
    match expression:
        case Literal(value=None):
            return True
        case Literal():
            return False
        case BoundColumnRef(scan_position=position):
            return position in sources
        case UnaryOp(operator=UnaryOperator.NOT):
            return truth_of(expression, sources) == _NULL
        case UnaryOp(operand=operand):
            # Arithmetic negation, and unary plus, are strict.
            return _definitely_null(operand, sources)
        case BinaryOp(operator=operator, left=left, right=right):
            if operator.is_logical or operator.is_comparison:
                return truth_of(expression, sources) == _NULL
            # Arithmetic is strict, including division: NULL propagates before
            # the operator runs, which is why `b.x / 0` is NULL and not an error
            # when `b.x` is NULL.
            return _definitely_null(left, sources) or _definitely_null(right, sources)
        case IsNullTest():
            return False
    return False


def _combine(left: Truth, right: Truth, table: dict[tuple[Truth, Truth], Truth]) -> Truth:
    """Apply a two-argument truth table pointwise over two sets of outcomes."""
    out = Truth(0)
    for first in left:
        for second in right:
            out |= table[(first, second)]
    return out


def _map(operand: Truth, table: dict[Truth, Truth]) -> Truth:
    out = Truth(0)
    for value in operand:
        out |= table[value]
    return out
