"""Cutting a failing case down to the smallest one that still fails.

A random failure is nearly worthless unshrunk. A case is one schema and sixteen
queries over two tables with a dozen columns and eight rows each; the thing that
is actually wrong is usually one query, one column and two rows. Shrinking is what
turns "seed 1731 disagrees" into something a person can read in ten seconds.

Two properties make it trustworthy.

**It keeps the *same* failure.** A candidate is accepted only when the smaller case
still produces a comparison with the identical
:meth:`~tests.differential.oracle.Comparison.signature`, not merely when it still
fails somehow. Without that, a shrinker wanders onto a second, easier bug and
reports a beautiful minimal case for something nobody was looking at. That is the
most common way a shrinker lies, and it costs one comparison per candidate to
prevent.

**Every reduction is on the spec, never on SQL text.** The SQL is re-rendered from
the reduced spec, so a shrunk case is still a case the generator could have
produced: it replays from its own seed-independent spec, it can be shrunk again,
and it pastes into a regression test verbatim. A shrinker that edited SQL strings
would eventually produce something neither engine parses and then report *that*.

Reductions are ordered biggest-win-first, because the first one is worth all the
others put together: dropping fifteen of sixteen queries makes every later step
sixteen times cheaper.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace

from tests.differential.generator import Case, SchemaSpec, TableSpec
from tests.differential.oracle import Comparison

__all__ = ["MAX_STEPS", "reductions", "shrink", "size"]

#: Each candidate costs about a millisecond and a half, so this is under a second
#:, which is why shrinking runs inside the failing test rather than in a separate
#: tool a developer has to know to invoke.
MAX_STEPS = 400

Runner = Callable[[Case], list[Comparison]]


def size(instance: Case) -> int:
    """A strictly decreasing measure, so the loop is guaranteed to terminate."""
    return (
        len(instance.queries)
        + sum(
            1 + len(table.columns) + len(table.rows) + len(table.indexed)
            for table in instance.schema.tables
        )
        + sum(len(query.sql) for query in instance.queries) // 40
    )


def shrink(
    instance: Case,
    target: tuple[str, ...],
    run: Runner,
    *,
    max_steps: int = MAX_STEPS,
) -> tuple[Case, int]:
    """The smallest case reachable that still fails with signature ``target``."""
    best = instance
    steps = 0
    improved = True
    while improved and steps < max_steps:
        improved = False
        for candidate in reductions(best):
            if steps >= max_steps:
                break
            steps += 1
            if not _still_fails(candidate, target, run):
                continue
            if size(candidate) < size(best):
                best = candidate
                improved = True
                break
    return best, steps


def _still_fails(candidate: Case, target: tuple[str, ...], run: Runner) -> bool:
    try:
        comparisons = run(candidate)
    except Exception:
        # A reduction that breaks the setup is simply rejected, not reported.
        return False
    return any(item.fails and item.signature() == target for item in comparisons)


def reductions(instance: Case) -> Iterator[Case]:
    """Every one-step simplification, biggest expected win first."""
    yield from _fewer_queries(instance)
    yield from _fewer_rows(instance)
    yield from _fewer_indexes(instance)
    yield from _weaker_constraints(instance)
    yield from _simpler_values(instance)


def _fewer_queries(instance: Case) -> Iterator[Case]:
    """One query at a time. Immediate, and it makes everything after it cheaper."""
    if len(instance.queries) <= 1:
        return
    for index in range(len(instance.queries)):
        yield replace(instance, queries=(instance.queries[index],))


def _fewer_rows(instance: Case) -> Iterator[Case]:
    """Delta debugging: halves first, then single rows.

    Halves first because a case with eight rows usually needs one or two, and
    removing them one at a time would take eight accepted steps to get there while
    halving takes three.
    """
    for position, table in enumerate(instance.schema.tables):
        rows = table.rows
        if not rows:
            continue
        half = len(rows) // 2
        candidates = []
        if half:
            candidates.extend([rows[:half], rows[half:]])
        candidates.extend(rows[:index] + rows[index + 1 :] for index in range(len(rows)))
        for kept in candidates:
            yield _with_table(
                instance, position, replace(table, rows=_renumber(table, kept))
            )


def _renumber(table: TableSpec, rows: tuple[tuple[object, ...], ...]) -> tuple:
    """Keep the primary key dense after a row is dropped.

    The key is an index, so removing row 1 of three would otherwise leave keys
    0 and 2, which is legal, but makes a shrunk case read as though the gap
    mattered. Renumbering keeps the repro about the thing that is wrong.
    """
    position = next(
        (index for index, column in enumerate(table.columns) if column.primary_key), None
    )
    if position is None:
        return rows
    return tuple(
        tuple(index if column == position else value for column, value in enumerate(row))
        for index, row in enumerate(rows)
    )


def _fewer_indexes(instance: Case) -> Iterator[Case]:
    for position, table in enumerate(instance.schema.tables):
        for dropped in table.indexed:
            kept = tuple(name for name in table.indexed if name != dropped)
            yield _with_table(instance, position, replace(table, indexed=kept))


def _weaker_constraints(instance: Case) -> Iterator[Case]:
    """Drop a ``NOT NULL``. The primary key stays. The rows depend on it."""
    for position, table in enumerate(instance.schema.tables):
        for index, column in enumerate(table.columns):
            if column.nullable or column.primary_key:
                continue
            columns = (
                *table.columns[:index],
                replace(column, nullable=True),
                *table.columns[index + 1 :],
            )
            yield _with_table(instance, position, replace(table, columns=columns))


#: What each type shrinks toward. A repro reads better with zeros and empty
#: strings in it than with 2.25 and 'ab'.
_SIMPLEST = {"INTEGER": 0, "FLOAT": 0.0, "TEXT": "", "BOOLEAN": False}


def _simpler_values(instance: Case) -> Iterator[Case]:
    """Move one cell toward the simplest member of its domain, or toward NULL.

    Both directions are tried. Turning a value into NULL is what isolates a
    three-valued-logic bug; turning a NULL into a value is what proves one is
    *not* the cause.
    """
    for position, table in enumerate(instance.schema.tables):
        for row_index, row in enumerate(table.rows):
            for column_index, column in enumerate(table.columns):
                if column.primary_key:
                    continue
                current = row[column_index]
                for target in (_SIMPLEST[column.type], None):
                    if current == target and (current is None) == (target is None):
                        continue
                    if target is None and not column.nullable:
                        continue
                    changed = (*row[:column_index], target, *row[column_index + 1 :])
                    rows = (*table.rows[:row_index], changed, *table.rows[row_index + 1 :])
                    yield _with_table(instance, position, replace(table, rows=rows))


def _with_table(instance: Case, position: int, table: TableSpec) -> Case:
    tables = (
        *instance.schema.tables[:position],
        table,
        *instance.schema.tables[position + 1 :],
    )
    return replace(instance, schema=SchemaSpec(tables))
