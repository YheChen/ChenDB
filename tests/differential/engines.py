"""The two engines, behind one interface.

Both adapters answer the same question ("run this, and tell me what happened")
and both refuse to interpret. An :class:`Outcome` records whether the statement
succeeded, the rows, the column names, a DML row count, and the *class* of any
error. Never the error message, for anything the oracle branches on: two engines
are entitled to different words for the same refusal, and comparing prose would
turn every improved error message into a test failure.

Setup and query are separate calls on purpose. ChenDB runs a whole script in one
implicit transaction and rolls all of it back if any statement raises, so folding
the setup into the query would mean one bad query took the schema with it and
every later comparison in the case compared two empty databases. The same
property is what makes DML testable: each mutation runs inside ``BEGIN`` …
``ROLLBACK``, so one schema serves many DML queries and no query can affect the
next.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine import Database
from engine.errors import ChenDBError
from engine.executor import execute_script
from tests.differential.dialect import normalise_rows
from tests.differential.generator import CHENDB, SQLITE, Case, Query

__all__ = ["Outcome", "chendb_outcomes", "sqlite_outcomes"]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one engine did with one statement."""

    ok: bool
    rows: tuple[tuple[Any, ...], ...] = ()
    columns: tuple[str, ...] = ()
    row_count: int | None = None
    """Rows a DML statement changed. ``None`` for a ``SELECT``."""
    state: tuple[tuple[Any, ...], ...] = ()
    """For DML: every table read back afterwards, so the *effect* is compared."""
    error_class: str = ""
    error_message: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


@dataclass(slots=True)
class _Failure:
    """A setup that did not apply. Not a divergence, a broken fixture."""

    engine: str
    statement: str
    error: str


@dataclass(slots=True)
class Run:
    outcomes: list[Outcome] = field(default_factory=list)
    setup_failure: _Failure | None = None


def _state_sql(instance: Case) -> list[str]:
    """``SELECT * ... ORDER BY <key>`` per table, a total order, so comparable.

    The primary key is unique and non-null by construction, so this read-back has
    exactly one right answer and can be compared as a sequence. It is what makes
    a DML comparison about the rows left behind rather than only about a count.
    """
    return [
        f"SELECT * FROM {table.name} ORDER BY {table.key.name};"
        for table in instance.schema.tables
    ]


# --------------------------------------------------------------------------
# ChenDB
# --------------------------------------------------------------------------


def chendb_outcomes(instance: Case, workspace: Path) -> Run:
    """Run every query of ``instance`` against a fresh ChenDB database."""
    run = Run()
    path = workspace / f"case{instance.seed}.chendb"
    # The shrinker runs the same case in the same workspace over and over, so a
    # file left by the previous attempt would make every CREATE TABLE fail and
    # the shrink report "the generated setup does not apply" instead of the
    # divergence it was chasing. A fresh database per run, always.
    for stale in workspace.glob(f"{path.name}*"):
        stale.unlink()
    with Database.open(path) as database:
        for statement in instance.schema.setup(CHENDB):
            try:
                execute_script(statement, database)
            except ChenDBError as error:
                run.setup_failure = _Failure(
                    CHENDB, statement, f"{type(error).__name__}: {error}"
                )
                return run

        for item in instance.queries:
            run.outcomes.append(_chendb_one(item, database, instance))
    return run


def _chendb_one(item: Query, database: Database, instance: Case) -> Outcome:
    if item.kind != "select":
        return _chendb_dml(item, database, instance)
    try:
        results = execute_script(item.sql, database)
    except ChenDBError as error:
        return Outcome(ok=False, error_class=type(error).__name__, error_message=str(error))
    last = results[-1]
    return Outcome(
        ok=True,
        rows=normalise_rows(last.rows),
        columns=tuple(column.name for column in last.columns),
    )


def _chendb_dml(item: Query, database: Database, instance: Case) -> Outcome:
    """A mutation, inside a transaction that is always rolled back."""
    database.begin()
    try:
        try:
            results = execute_script(item.sql, database, atomic=False)
        except ChenDBError as error:
            return Outcome(
                ok=False, error_class=type(error).__name__, error_message=str(error)
            )
        state: list[tuple[Any, ...]] = []
        for read_back in _state_sql(instance):
            state.extend(normalise_rows(execute_script(read_back, database)[-1].rows))
        return Outcome(
            ok=True, row_count=results[-1].stats.rows_affected, state=tuple(state)
        )
    finally:
        if database.active_transaction is not None:
            database.rollback()


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------


def sqlite_outcomes(instance: Case) -> Run:
    """The same queries against SQLite, in memory.

    ``isolation_level=None`` turns off the driver's implicit transaction
    management, which is what makes an explicit ``BEGIN`` behave the way ChenDB's
    does, without it, ``sqlite3`` opens and commits transactions on its own and
    the DML rollback would not roll anything back.
    """
    run = Run()
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in instance.schema.setup(SQLITE):
            try:
                connection.executescript(statement)
            except sqlite3.Error as error:
                run.setup_failure = _Failure(
                    SQLITE, statement, f"{type(error).__name__}: {error}"
                )
                return run

        for item in instance.queries:
            run.outcomes.append(_sqlite_one(item, connection, instance))
    finally:
        connection.close()
    return run


def _sqlite_one(item: Query, connection: sqlite3.Connection, instance: Case) -> Outcome:
    if item.kind != "select":
        return _sqlite_dml(item, connection, instance)
    try:
        cursor = connection.execute(item.sqlite_sql)
        rows = cursor.fetchall()
    except sqlite3.Error as error:
        return Outcome(ok=False, error_class=type(error).__name__, error_message=str(error))
    columns = tuple(column[0] for column in cursor.description or ())
    return Outcome(ok=True, rows=normalise_rows(rows), columns=columns)


def _sqlite_dml(item: Query, connection: sqlite3.Connection, instance: Case) -> Outcome:
    connection.execute("BEGIN")
    try:
        try:
            cursor = connection.execute(item.sqlite_sql)
        except sqlite3.Error as error:
            return Outcome(
                ok=False, error_class=type(error).__name__, error_message=str(error)
            )
        count = cursor.rowcount
        state: list[tuple[Any, ...]] = []
        for read_back in _state_sql(instance):
            state.extend(normalise_rows(connection.execute(read_back).fetchall()))
        return Outcome(ok=True, row_count=count, state=tuple(state))
    finally:
        connection.execute("ROLLBACK")
