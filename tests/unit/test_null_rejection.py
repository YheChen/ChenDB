"""Proving a predicate cannot be TRUE once a table's columns are all NULL.

This is the analysis the whole of Milestone 19 stands on, so it is tested in the
one direction that can be wrong dangerously. Saying "does not reject" when a
predicate does costs a rewrite. Saying "rejects" when it does not turns an outer
join into an inner one and silently deletes rows, and no later test would
attribute the missing rows to the optimiser.

So :func:`test_a_rejected_predicate_is_never_true_on_a_null_extended_row` is
exhaustive rather than illustrative: every predicate below, against every
assignment of values that can be made to the preserved side, with the answer
computed by the engine's real evaluator rather than by a second opinion written
here.
"""

from __future__ import annotations

import itertools
from typing import Any, Final

import pytest

from engine.errors import EvaluationError
from engine.executor.binder import Scope, ScopeEntry, bind_expression
from engine.executor.expression import evaluate
from engine.optimizer.nullability import ANY, Truth, rejects_nulls, truth_of
from engine.parser.ast import Expression
from engine.parser.parser import parse_statement
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

#: The preserved side of the join. Its values are unknown to the analysis.
LEFT = Schema.of(
    Column("n", DataType.INTEGER),
    Column("flag", DataType.BOOLEAN),
)
#: The NULL-supplied side. Every column of it is NULL in a preserved row.
RIGHT = Schema.of(
    Column("x", DataType.INTEGER),
    Column("ok", DataType.BOOLEAN),
)

SCOPE: Final = Scope(
    (
        ScopeEntry("a", "a", LEFT, offset=0, position=0),
        ScopeEntry("b", "b", RIGHT, offset=len(LEFT), position=1),
    )
)

#: ``b``, by scan position. What an outer join above would fill with NULLs.
NULLED: Final = frozenset({1})

#: Values for ``a.n`` and ``a.flag``. Small, because the property below is
#: exhaustive over their product and every predicate at once, and because the
#: interesting values of an unknown are "something, nothing, and a boundary".
LEFT_VALUES: Final = ((None, 0, 1, -1), (None, True, False))


def bound(sql: str) -> Expression:
    """A WHERE predicate, bound against the two-table scope above."""
    statement = parse_statement(f"SELECT * FROM a WHERE {sql}")
    assert statement.where is not None
    return bind_expression(statement.where, SCOPE)


def rejects(sql: str) -> bool:
    return rejects_nulls([bound(sql)], NULLED)


# -- the truth tables are SQL's, not a second opinion -------------------------

_LITERALS: Final = {"TRUE": Truth.TRUE, "FALSE": Truth.FALSE, "NULL": Truth.UNKNOWN}


@pytest.mark.parametrize("left", _LITERALS)
@pytest.mark.parametrize("right", _LITERALS)
@pytest.mark.parametrize("operator", ["AND", "OR"])
def test_the_abstract_tables_match_the_evaluator(left: str, right: str, operator: str):
    """Nine rows each, checked against the engine rather than against a belief.

    The tables in :mod:`engine.optimizer.nullability` are a second copy of SQL's
    three-valued logic, and a second copy is a thing that can drift. Rather than
    assert the copy is what this file thinks it should be, run the same
    expression through :func:`evaluate` and compare. If either changes alone,
    this goes red.
    """
    concrete = evaluate(bound(f"{left} {operator} {right}"), ())
    abstract = truth_of(bound(f"{left} {operator} {right}"), NULLED)
    assert abstract == _LITERALS[{True: "TRUE", False: "FALSE", None: "NULL"}[concrete]]


@pytest.mark.parametrize("operand", _LITERALS)
def test_the_abstract_not_matches_the_evaluator(operand: str):
    concrete = evaluate(bound(f"NOT {operand}"), ())
    abstract = truth_of(bound(f"NOT {operand}"), NULLED)
    assert abstract == _LITERALS[{True: "TRUE", False: "FALSE", None: "NULL"}[concrete]]


# -- the property that matters ------------------------------------------------

#: Every predicate shape the analysis claims to understand, plus several it does
#: not, because the ones it does not are exactly where an unsound rule would
#: hide. Each is checked below against every assignment to the preserved side.
PREDICATES: Final = (
    # strict comparisons against the NULL-supplied side
    "b.x = 5",
    "b.x <> 5",
    "b.x > a.n",
    "a.n <= b.x",
    "b.x + 1 = 2",
    "b.x / 0 = 1",
    "-b.x = 0",
    "b.ok",
    "NOT b.ok",
    # the ones that can see a NULL, and so must not be rejected
    "b.x IS NULL",
    "b.ok IS NULL",
    "NOT (b.x IS NOT NULL)",
    "b.x IS NULL AND a.n = 1",
    "b.x IS NULL OR b.x = 5",
    # IS NOT NULL is the mirror image and does reject
    "b.x IS NOT NULL",
    "NOT (b.x IS NULL)",
    # combinations
    "b.x = 5 AND a.n = 1",
    "b.x = 5 OR a.n = 1",
    "b.x = 5 OR b.ok",
    "(b.x = 5 OR a.n = 1) AND b.ok IS NOT NULL",
    "b.x IS NULL AND b.x IS NOT NULL",
    "NOT (b.x = 5 AND a.n = 1)",
    "NOT (b.x = 5 OR a.n = 1)",
    # nothing to do with the NULL-supplied side at all
    "a.n = 1",
    "a.flag",
    "a.n IS NULL",
    "TRUE",
    "FALSE",
    "NULL",
)


def _rows() -> list[tuple[Any, ...]]:
    """Every joined row an outer join could preserve: ``a`` anything, ``b`` NULL."""
    return [(*left, None, None) for left in itertools.product(*LEFT_VALUES)]


@pytest.mark.parametrize("sql", PREDICATES)
def test_a_rejected_predicate_is_never_true_on_a_null_extended_row(sql: str):
    """The soundness property, stated as directly as it can be.

    If the analysis says a predicate rejects NULLs, then no row with the
    NULL-supplied side all NULL may make it TRUE. A counterexample here is a
    query that would lose rows.

    An evaluation that *raises* is not a counterexample. The row does not reach
    the output either way, and ChenDB raises on a comparison it refuses rather
    than guessing, so the alternative would be to generate only predicates that
    never fail, which is a smaller test for no gain.
    """
    predicate = bound(sql)
    if not rejects_nulls([predicate], NULLED):
        pytest.skip("not rejected, so there is nothing to prove")
    for row in _rows():
        try:
            outcome = evaluate(predicate, row)
        except EvaluationError:
            continue
        assert outcome is not True, (
            f"{sql!r} was proved to reject NULLs, but it is TRUE on {row!r}. "
            f"An outer join above this predicate would be rewritten to an inner "
            f"join and would lose that row."
        )


@pytest.mark.parametrize("sql", PREDICATES)
def test_the_abstract_outcome_contains_the_real_one(sql: str):
    """The stronger property the one above is a corollary of.

    Abstract interpretation is sound when its answer is a *superset* of what can
    really happen. Checking the superset directly, rather than only the
    TRUE-is-absent corner of it, is what catches a rule that is accidentally
    right on this predicate list and wrong on the next one added to it.
    """
    predicate = bound(sql)
    possible = truth_of(predicate, NULLED)
    for row in _rows():
        try:
            outcome = evaluate(predicate, row)
        except EvaluationError:
            continue
        actual = {True: Truth.TRUE, False: Truth.FALSE, None: Truth.UNKNOWN}[outcome]
        assert actual in possible, (
            f"{sql!r} came out {outcome!r} on {row!r}, which is not in the "
            f"predicted {possible!r}. The analysis is unsound."
        )


# -- what it should and should not prove --------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "b.x = 5",
        "b.x <> 5",
        "b.x > 0",
        "b.x + 1 = 2",
        "b.x = a.n",
        "b.ok",
        "NOT b.ok",
        "b.x IS NOT NULL",
        "NOT (b.x IS NULL)",
        "b.x = 5 AND a.n = 1",
        "a.n = 1 AND b.x = 5",
        "b.x = 5 OR b.ok",
        "NOT (b.x = 5)",
        "b.x IS NULL AND b.x IS NOT NULL",
    ],
)
def test_these_reject_nulls(sql: str):
    assert rejects(sql), f"{sql!r} cannot be TRUE with b NULL, so it should reject"


@pytest.mark.parametrize(
    "sql",
    [
        # The anti-join idiom. Rewriting this one would break the single most
        # common reason anybody writes an outer join by hand.
        "b.x IS NULL",
        "b.ok IS NULL",
        # One survivable branch is enough.
        "b.x = 5 OR a.n = 1",
        "b.x IS NULL OR b.x = 5",
        # Nothing to do with b.
        "a.n = 1",
        "a.flag",
        "TRUE",
    ],
)
def test_these_do_not_reject_nulls(sql: str):
    assert not rejects(sql)


def test_a_predicate_on_the_other_side_proves_nothing_about_this_one():
    # `a.n = 1` rejects NULLs for `a`, and says nothing at all about `b`. Reading
    # the sources argument the wrong way round would make every WHERE on the
    # preserved side collapse its own outer join.
    predicate = bound("a.n = 1")
    assert rejects_nulls([predicate], frozenset({0}))
    assert not rejects_nulls([predicate], frozenset({1}))


@pytest.mark.parametrize("first", PREDICATES)
@pytest.mark.parametrize("second", ["b.x = 5", "b.x IS NULL", "a.n = 1"])
def test_a_conjunction_rejects_exactly_when_one_of_its_terms_does(first: str, second: str):
    """A sequence buys the right *interface*, not extra strength, and that is fine.

    ``AND`` is applied pointwise, and ``TRUE AND TRUE`` is ``TRUE``, so TRUE
    survives a conjunction exactly when both sides could produce it. Passing the
    WHERE and the ON clauses together therefore proves neither more nor less
    than asking about each in turn.

    Worth pinning rather than assuming, because the rule above reads as though
    combining evidence makes it stronger. It does not, and if that ever changes
    (a version that tracks which column each term constrains, say) this test is
    where the change announces itself.
    """
    terms = [bound(first), bound(second)]
    together = rejects_nulls(terms, NULLED)
    separately = any(rejects_nulls([term], NULLED) for term in terms)
    assert together == separately


def test_a_single_expression_can_see_a_contradiction_a_sequence_cannot():
    """The one place correlation does survive: inside one expression tree.

    Neither half of ``b.x IS NULL AND b.x IS NOT NULL`` is UNKNOWN. Each is a
    single known outcome, TRUE and FALSE, so combining them pointwise loses
    nothing and the ``AND`` is proved FALSE.
    """
    assert rejects("b.x IS NULL AND b.x IS NOT NULL")
    assert truth_of(bound("b.x IS NULL AND b.x IS NOT NULL"), NULLED) == Truth.FALSE


def test_no_predicates_proves_nothing():
    # Not vacuously true. Nothing is filtering, so every preserved row survives.
    assert not rejects_nulls([], NULLED)


def test_an_unrecognised_expression_is_assumed_to_be_anything():
    """The default has to be ANY, and this pins which way the default falls.

    A node the analysis has never seen (a function call today, a subquery or a
    CASE later) must come back as "could be anything", because a rewrite fires
    on the *absence* of TRUE. Defaulting the other way would mean every new
    expression node silently licences a rewrite until someone notices.
    """
    statement = parse_statement("SELECT COUNT(b.x) FROM a")
    call = statement.projections[0].expression
    assert truth_of(bind_expression(call, SCOPE), NULLED) == ANY
    assert not rejects_nulls([bind_expression(call, SCOPE)], NULLED)
