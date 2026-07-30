"""The same query, said twice: once to ChenDB and once to SQLite.

Two engines only disagree usefully once you have removed the ways they were never
going to agree. There are three of those, and they are different in kind. The
distinction is the whole discipline of this module:

**Notation.** SQLite has no ``FLOAT``; it has ``REAL``, and accepts ``FLOAT`` as
a type name with REAL affinity. Trivial.

**Representation.** SQLite has no boolean type at all: ``TRUE`` is the integer
``1``. ChenDB's ``BOOLEAN`` is a real type with a one-byte codec, so ``MIN(ok)``
is ``False`` here and ``0`` there. Neither is wrong, and a comparison that called
them different would never report anything else. That is :func:`normalise`.

**Semantics the standard leaves open.** Where NULLs sort. ChenDB puts them last
in ``ASC`` and first in ``DESC``, which is PostgreSQL's default and the opposite
of SQLite's. The tempting response is to stop generating ``ORDER BY`` over
nullable columns, which would give up exactly the corner most likely to hide a
bug. So SQLite is *asked* for ChenDB's order instead, with the ``NULLS LAST`` /
``NULLS FIRST`` modifiers it has had since 3.30, and the generated SQL says so
out loud, because a failing case is read by a human.

The rule, and it is the reason this module is small: **a translation is allowed
only when the difference is notation or representation, never when it is about
what the query means.** It is very easy to write a compatibility layer that
quietly repairs a disagreement instead of reporting it, and a differential
tester that does that is worse than no tester, it is a green tick over a bug.

Nothing here rewrites SQL text. An earlier draft did: ``to_sqlite_ddl`` ran
``str.replace(" FLOAT", " REAL")`` over the whole setup script, which would have
corrupted a row containing the *string* ``' FLOAT'`` on the SQLite side only, and
manufactured a divergence out of nothing. Both dialects are rendered from the
same spec instead (:meth:`~tests.differential.generator.TableSpec.ddl`), so there
is no text to get wrong.
"""

from __future__ import annotations

import math
from typing import Any, Final

__all__ = [
    "FLOAT_RELATIVE_TOLERANCE",
    "MINIMUM_SQLITE_VERSION",
    "NULL_ORDER_ASC",
    "NULL_ORDER_DESC",
    "canonical",
    "normalise",
    "normalise_rows",
    "sqlite_type_name",
    "values_agree",
]

#: What ChenDB does, spelled for SQLite. Appended to every generated sort key.
#: PostgreSQL's defaults, ChenDB follows PostgreSQL wherever it and SQLite
#: differ, which is this project's stated tie-breaker.
NULL_ORDER_ASC: Final = "NULLS LAST"
NULL_ORDER_DESC: Final = "NULLS FIRST"

#: ``NULLS LAST`` landed in SQLite 3.30. Below that the translation above is a
#: syntax error and the whole suite would be comparing the wrong thing, so
#: ``test_sqlite_is_new_enough`` *fails* rather than skips, ``sqlite3`` is in the
#: standard library and cannot be absent, so there is nothing to be lenient about.
MINIMUM_SQLITE_VERSION: Final = (3, 30, 0)

#: How close two floats must be to count as equal, *when* the oracle allows any
#: slack at all. A few parts in 10¹², not a comfortable epsilon: a tolerance is a
#: place for a bug to hide, so this is deliberately tighter than the error any
#: real bug would produce. See :func:`values_agree` for when it applies, which is
#: never, unless the caller asks.
FLOAT_RELATIVE_TOLERANCE: Final = 1e-12

#: Type names that differ. ``INTEGER`` and ``TEXT`` are the same word in both.
#: ``BOOLEAN`` is deliberately *not* translated: SQLite accepts the name (with
#: NUMERIC affinity) and understands ``TRUE``/``FALSE``, so the declaration
#: survives to document the intent and only the returned values need normalising.
_TYPE_NAMES: Final[dict[str, str]] = {"FLOAT": "REAL"}

#: Sort rank per Python type, so :func:`canonical` can order a column holding
#: several. Comparing across types would raise, and NULL compares with nothing.
_TYPE_RANK: Final[dict[type | None, int]] = {
    type(None): 0,
    bool: 1,
    int: 1,
    float: 1,
    str: 2,
}


def sqlite_type_name(chendb_type: str) -> str:
    """``FLOAT`` → ``REAL``; everything else unchanged."""
    return _TYPE_NAMES.get(chendb_type, chendb_type)


def normalise(value: Any) -> Any:
    """One value, in a form the two engines can be compared in.

    Booleans become integers rather than integers becoming booleans, and the
    direction is forced rather than chosen: every ``bool`` is a ``0`` or a ``1``,
    but ``2`` is not a boolean. Normalising the other way would make
    ``SUM(ok) = 2`` compare equal to ``True``.
    """
    if isinstance(value, bool):
        return int(value)
    return value


def normalise_rows(rows: Any) -> tuple[tuple[Any, ...], ...]:
    """:func:`normalise` over a whole result set."""
    return tuple(tuple(normalise(value) for value in row) for row in rows)


def values_agree(mine: Any, theirs: Any, *, tolerant: bool = False) -> bool:
    """Whether two normalised values are the same answer.

    Exact by default, including the Python type: after normalisation ``2`` and
    ``2.0`` are *different answers*, because a type-inference bug that returns an
    integer where a float is correct is a real bug and this is the only place it
    would ever be caught. Both engines agree on the cases that matter (
    ``AVG`` over integers is a float in both, ``SUM`` over integers an integer in
    both) so the strictness costs nothing and earns that.

    NULL equals only NULL. Not ``0``, not ``''``, in either direction. "NULL
    where I expected a value" is the most common wrong answer a query engine
    gives, and absorbing it would blind the tester to its best finding.

    ``tolerant`` is for floats that came out of a *reduction*. A ``SUM`` or
    ``AVG`` over a FLOAT column, or an expression with more than one float
    operand, where IEEE addition is not associative and the two engines are
    entitled to fold in a different order. It is opt-in per column, decided from
    the query's shape rather than from whether the comparison happened to fail,
    and every use is counted and reported so a tolerance can never start quietly
    absorbing something.
    """
    if mine is None or theirs is None:
        return mine is None and theirs is None
    if type(mine) is not type(theirs):
        return False
    if isinstance(mine, float):
        if mine == theirs:
            return True
        if not tolerant:
            return False
        return math.isclose(mine, theirs, rel_tol=FLOAT_RELATIVE_TOLERANCE, abs_tol=0.0)
    return bool(mine == theirs)


def canonical(row: tuple[Any, ...]) -> tuple[tuple[int, str], ...]:
    """A total order over rows, for comparing two result sets as multisets.

    Sorts on ``(type rank, text)`` rather than on the values, so a column holding
    both a number and a NULL cannot raise a ``TypeError`` inside the comparison.
    The order is arbitrary but *stable*, which is all a multiset needs.

    The invariant that matters: **this must never separate two values
    :func:`values_agree` calls equal.** It did, once, and the tester reported three
    divergences against the engine before the engine was the problem. ``-0.0`` and
    ``0.0`` are equal in SQL (both engines agree, and so does ``==``) but their
    ``repr`` differs, so a tie run containing ``a.f * 0.0`` was grouped one way for
    ChenDB and another for SQLite and the ordering check fired. A comparison key
    that is finer than the equality it serves invents differences.
    """
    return tuple((_TYPE_RANK.get(type(value), 3), _key_text(value)) for value in row)


def _key_text(value: Any) -> str:
    if isinstance(value, float):
        # `-0.0 + 0.0` is `0.0`, which is the point: it folds the one float pair
        # whose reprs differ while their values do not.
        return repr(value + 0.0)
    return repr(value)
