"""The registry cannot rot, and cannot become an escape hatch.

Every constraint the registry's docstring claims is checked here rather than left
to anyone's discipline. The one that matters most is
:func:`test_the_registered_divergence_is_still_a_divergence`: it runs each entry's
canonical example on both engines and asserts the recorded outcome pair *still*
occurs. The day ChenDB stops raising on division by zero, that test goes red and
the entry has to be deleted — which is exactly the mechanism the project's stale
CLI milestone string never had, and the reason it sat a release behind.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engine import Database
from engine.errors import ChenDBError
from engine.executor import execute_script
from tests.differential.dialect import sqlite_type_name
from tests.differential.engines import Outcome
from tests.differential.registry import ENTRIES, MAXIMUM_ENTRIES, Entry, Kind

IDS = [entry.rule for entry in ENTRIES]


def _chendb(entry: Entry, workspace: Path) -> Outcome:
    path = workspace / "registry.chendb"
    with Database.open(path) as database:
        for statement in entry.setup.splitlines():
            if statement.strip():
                execute_script(statement, database)
        try:
            results = execute_script(entry.sql, database)
        except ChenDBError as error:
            return Outcome(
                ok=False, error_class=type(error).__name__, error_message=str(error)
            )
        return Outcome(ok=True, rows=results[-1].rows)


def _sqlite(entry: Entry) -> Outcome:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        setup = "\n".join(
            line.replace(" FLOAT", f" {sqlite_type_name('FLOAT')}")
            for line in entry.setup.splitlines()
        )
        connection.executescript(setup)
        try:
            rows = connection.execute(entry.sql).fetchall()
        except sqlite3.Error as error:
            return Outcome(
                ok=False, error_class=type(error).__name__, error_message=str(error)
            )
        return Outcome(ok=True, rows=tuple(rows))
    finally:
        connection.close()


# -- the list cannot rot -----------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_the_registered_divergence_is_still_a_divergence(entry: Entry, tmp_path: Path):
    """The example still behaves the way the entry says it does.

    A ``RESTRICTION`` is excluded: it records something the generator refuses to
    emit, so both engines succeed and there is no divergence to reproduce — the
    entry's job is to document a coverage decision, not an outcome.
    """
    if entry.kind is Kind.RESTRICTION:
        pytest.skip("a restriction documents what is not generated, not an outcome")

    mine = _chendb(entry, tmp_path)
    theirs = _sqlite(entry)
    assert entry.matches(mine, theirs), (
        f"{entry.rule} no longer reproduces, so the entry is stale and must go:\n"
        f"  expected  chendb={entry.chendb}  sqlite={entry.sqlite}\n"
        f"  got       chendb={'ok' if mine.ok else 'err:' + mine.error_class}"
        f"  sqlite={'ok' if theirs.ok else 'err:' + theirs.error_class}\n"
        f"  chendb said: {mine.error_message or mine.rows}\n"
        f"  sqlite said: {theirs.error_message or theirs.rows}"
    )


# -- the constraints on what may be registered -------------------------------


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_a_divergence_entry_excuses_an_error_and_never_a_value(entry: Entry):
    """Constraint 1. Exactly one side must have erred.

    There is deliberately nowhere to record "both returned rows and the rows
    differed". That is what the tester exists to find, and no wrong row is fine.
    """
    if entry.kind is Kind.RESTRICTION:
        return
    sides = (entry.chendb, entry.sqlite)
    assert sum(side != "ok" for side in sides) == 1, (
        f"{entry.rule}: a divergence entry must have exactly one erroring side; "
        f"a both-ok row mismatch is a bug and has nowhere to be filed"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_every_entry_names_at_most_one_error_class(entry: Entry):
    """Constraint 4. An entry that would accept two must be split.

    So its reason has to be true of one thing, which is what stops an entry
    quietly widening into a catch-all.
    """
    assert len(entry.error_classes) <= 1, entry.rule


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_no_entry_is_classified_as_a_bug(entry: Entry):
    """There is no ``BUG`` classification, by construction.

    A bug cannot be filed here instead of being fixed. The honest way to park an
    undiagnosed one is a named ``@pytest.mark.xfail(strict=True)`` test, which
    shows up in every run's summary and goes red the day it starts passing.
    """
    assert entry.classification in ("DELIBERATE", "HARNESS"), entry.rule


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_every_entry_says_what_postgres_does(entry: Entry):
    """ChenDB follows PostgreSQL where it and SQLite differ.

    That is the project's stated tie-breaker, so an entry whose reason does not
    mention PostgreSQL is one nobody has actually thought through — it has only
    observed that the two disagree.
    """
    assert "PostgreSQL" in entry.reason, (
        f"{entry.rule}: say what PostgreSQL does, or why the comparison does not "
        f"apply — otherwise the entry records an observation, not a decision"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_every_entry_carries_a_runnable_example(entry: Entry):
    assert entry.setup.strip() and entry.sql.strip(), entry.rule
    assert entry.rule == entry.rule.lower().replace(" ", "-"), (
        f"{entry.rule}: rules are kebab-case, so they read as identifiers in a report"
    )


def test_the_rules_are_unique():
    assert len(set(IDS)) == len(IDS)


def test_the_registry_is_small():
    """Constraint 5. A cap, so the list stays honest and not just each entry.

    A register that only ever grows is an escape hatch however carefully each line
    is worded. Hitting this forces a conversation rather than an append.
    """
    assert len(ENTRIES) <= MAXIMUM_ENTRIES, (
        f"{len(ENTRIES)} entries. Before raising the cap: is the newest one a rule "
        f"about semantics, or a bug that has not been diagnosed?"
    )
