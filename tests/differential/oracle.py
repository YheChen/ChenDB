"""Deciding whether two answers are the same answer.

The whole difficulty is that "same" is not "equal". Two engines running the same
correct query can legitimately return rows in a different order, and the oracle
must not confuse that with a wrong row — while still catching a sort that does not
sort. The rule it uses is: **the query's own shape decides how it is compared,
never the data.** A comparison that inspected the results to choose its own
strictness could always find a reading in which the two agree.

    no ORDER BY          → multiset, plus exact cardinality
    ORDER BY, total      → exact sequence
    ORDER BY, with ties  → three things, all of them defined

That last row is where the care is. With ties, the sequence of *rows* is
undefined, but three properties are not: the sequence of sort-key values, the
multiset of rows, and — the one that matters — the multiset of rows *within each
run of equal keys*. Without the third, a sort that carries a row across a group
boundary satisfies the first two and passes. It is four lines of
:func:`itertools.groupby` and it is the difference between testing the ordering
and merely testing that nothing was lost.

Errors are four cases, not two, and they are not symmetric:

    both ok         compare the values
    both error      agree — weakly; the classes are recorded, messages ignored
    ChenDB only     suspicious: strictness (registered) or a bug
    SQLite only     a *harness* failure, and never excusable

A SQLite-only error almost always means the generator emitted something outside
SQLite's dialect, which silently narrows coverage — a generator whose output the
reference engine refuses is testing less than it claims. That must never be
registrable, so it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby
from typing import Any

from tests.differential.dialect import canonical, values_agree
from tests.differential.engines import Outcome
from tests.differential.generator import Query

__all__ = ["Comparison", "Verdict", "compare"]


class Verdict(StrEnum):
    AGREE = "agree"
    AGREE_WITHIN_TOLERANCE = "agree-within-tolerance"
    REGISTERED = "registered"
    DIVERGE = "diverge"
    CHENDB_ONLY_ERROR = "chendb-only-error"
    SQLITE_ONLY_ERROR = "sqlite-only-error"


_FAILING = frozenset(
    {Verdict.DIVERGE, Verdict.CHENDB_ONLY_ERROR, Verdict.SQLITE_ONLY_ERROR}
)


@dataclass(frozen=True, slots=True)
class Comparison:
    """One query, both outcomes, and the verdict."""

    query: Query
    mine: Outcome
    theirs: Outcome
    verdict: Verdict
    detail: str = ""
    rule: str = ""
    """The registry entry that excused this, if any."""

    @property
    def fails(self) -> bool:
        return self.verdict in _FAILING

    def signature(self) -> tuple[str, ...]:
        """A short, stable identity for *this kind of* failure.

        The shrinker accepts a reduction only when the smaller case still
        produces this exact signature. Without that it drifts onto a different
        bug and reports a minimal case for something nobody was looking at, which
        is the most common way a shrinker lies.
        """
        if self.verdict in (Verdict.CHENDB_ONLY_ERROR, Verdict.SQLITE_ONLY_ERROR):
            return (str(self.verdict), self.mine.error_class or self.theirs.error_class)
        return (str(self.verdict), self.query.shape, self.detail.split(":")[0])


def compare(query: Query, mine: Outcome, theirs: Outcome) -> Comparison:
    """The verdict for one query. Knows nothing about the registry."""
    if mine.failed and theirs.failed:
        # Both refused. Recorded, not compared: the two are entitled to different
        # words, and even to different classes, for the same refusal.
        return Comparison(
            query,
            mine,
            theirs,
            Verdict.AGREE,
            detail=f"both refused: {mine.error_class} / {theirs.error_class}",
        )
    if mine.failed:
        return Comparison(
            query, mine, theirs, Verdict.CHENDB_ONLY_ERROR, detail=mine.error_class
        )
    if theirs.failed:
        return Comparison(
            query, mine, theirs, Verdict.SQLITE_ONLY_ERROR, detail=theirs.error_class
        )

    if query.kind != "select":
        return _compare_dml(query, mine, theirs)
    return _compare_rows(query, mine, theirs)


def _compare_dml(query: Query, mine: Outcome, theirs: Outcome) -> Comparison:
    """A mutation is compared on its effect first, its row count second.

    The count is the cheap signal and the state is the decisive one: an ``UPDATE``
    that reports 3 rows and writes the wrong value to all three has the right
    count.
    """
    if mine.row_count != theirs.row_count:
        return Comparison(
            query,
            mine,
            theirs,
            Verdict.DIVERGE,
            detail=f"row_count: {mine.row_count} vs {theirs.row_count}",
        )
    verdict, detail = _sequences_agree(mine.state, theirs.state, tolerant=frozenset())
    if verdict is not None:
        return Comparison(query, mine, theirs, Verdict.DIVERGE, detail=f"state {detail}")
    return Comparison(query, mine, theirs, Verdict.AGREE)


def _compare_rows(query: Query, mine: Outcome, theirs: Outcome) -> Comparison:
    if len(mine.columns) != len(theirs.columns):
        return Comparison(
            query,
            mine,
            theirs,
            Verdict.DIVERGE,
            detail=f"columns: {len(mine.columns)} vs {len(theirs.columns)}",
        )
    if mine.columns != theirs.columns:
        # Only comparable because every projection is aliased `c0..cn`. Unaliased,
        # the two engines name computed columns differently by design.
        return Comparison(
            query,
            mine,
            theirs,
            Verdict.DIVERGE,
            detail=f"column names: {mine.columns} vs {theirs.columns}",
        )
    if len(mine.rows) != len(theirs.rows):
        return Comparison(
            query,
            mine,
            theirs,
            Verdict.DIVERGE,
            detail=f"row count: {len(mine.rows)} vs {len(theirs.rows)}",
        )

    tolerant = query.tolerant_columns

    if query.total_order:
        detail = _sequence_detail(mine.rows, theirs.rows, tolerant)
        if detail:
            return Comparison(query, mine, theirs, Verdict.DIVERGE, detail=detail)
        return _tolerance_verdict(query, mine, theirs, tolerant)

    problem, detail = _sequences_agree(mine.rows, theirs.rows, tolerant, multiset=True)
    if problem is not None:
        return Comparison(query, mine, theirs, Verdict.DIVERGE, detail=f"rows {detail}")

    if query.sort_key_indices:
        keys_mine = _keys(mine.rows, query.sort_key_indices)
        keys_theirs = _keys(theirs.rows, query.sort_key_indices)
        if keys_mine != keys_theirs:
            return Comparison(
                query,
                mine,
                theirs,
                Verdict.DIVERGE,
                detail=f"sort keys: {keys_mine} vs {keys_theirs}",
            )
        if problem := _runs_agree(query, mine.rows, theirs.rows, tolerant):
            return Comparison(query, mine, theirs, Verdict.DIVERGE, detail=problem)

    return _tolerance_verdict(query, mine, theirs, tolerant)


def _tolerance_verdict(
    query: Query, mine: Outcome, theirs: Outcome, tolerant: frozenset[int]
) -> Comparison:
    """``AGREE``, or ``AGREE_WITHIN_TOLERANCE`` if any slack was load-bearing.

    Reported rather than absorbed. Every run prints how many comparisons needed
    the tolerance, so the day it starts rescuing something the number moves off
    zero instead of staying silent.
    """
    if not tolerant:
        return Comparison(query, mine, theirs, Verdict.AGREE)
    for left, right in zip(mine.rows, theirs.rows, strict=False):
        for index in tolerant:
            if index < len(left) and not values_agree(left[index], right[index]):
                return Comparison(query, mine, theirs, Verdict.AGREE_WITHIN_TOLERANCE)
    return Comparison(query, mine, theirs, Verdict.AGREE)


def _keys(
    rows: tuple[tuple[Any, ...], ...], indices: tuple[int, ...]
) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row[index] for index in indices) for row in rows)


def _runs_agree(
    query: Query,
    mine: tuple[tuple[Any, ...], ...],
    theirs: tuple[tuple[Any, ...], ...],
    tolerant: frozenset[int],
) -> str:
    """Within each run of equal sort keys, the rows must be the same multiset.

    This is what makes a tie-tolerant comparison a real test. The sort-key
    sequence being right and the row multiset being right are both satisfied by a
    sort that moved one row into the wrong tie group.
    """
    indices = query.sort_key_indices

    def key(row: tuple[Any, ...]) -> tuple[tuple[int, str], ...]:
        return canonical(tuple(row[index] for index in indices))

    left_runs = [sorted(map(canonical, group)) for _, group in groupby(mine, key=key)]
    right_runs = [sorted(map(canonical, group)) for _, group in groupby(theirs, key=key)]
    if left_runs != right_runs:
        return "rows are ordered differently within a run of equal sort keys"
    return ""


def _sequence_detail(
    mine: tuple[tuple[Any, ...], ...],
    theirs: tuple[tuple[Any, ...], ...],
    tolerant: frozenset[int],
) -> str:
    for position, (left, right) in enumerate(zip(mine, theirs, strict=False)):
        for column, (one, other) in enumerate(zip(left, right, strict=False)):
            if not values_agree(one, other, tolerant=column in tolerant):
                return f"row {position} col c{column}: {one!r} vs {other!r}"
    return ""


def _sequences_agree(
    mine: tuple[tuple[Any, ...], ...],
    theirs: tuple[tuple[Any, ...], ...],
    tolerant: frozenset[int],
    *,
    multiset: bool = False,
) -> tuple[str | None, str]:
    if len(mine) != len(theirs):
        return "count", f"count: {len(mine)} vs {len(theirs)}"
    left, right = (
        (sorted(mine, key=canonical), sorted(theirs, key=canonical))
        if multiset
        else (list(mine), list(theirs))
    )
    detail = _sequence_detail(tuple(left), tuple(right), tolerant)
    return (None, "") if not detail else ("value", detail)
