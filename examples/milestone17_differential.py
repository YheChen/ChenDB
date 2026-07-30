#!/usr/bin/env python3
"""A narrated tour of Milestone 17: asking SQLite whether ChenDB is right.

    python examples/milestone17_differential.py

Five things: the seven bugs a second engine found that 1,243 hand-written tests
did not, why a test can agree with a bug, why a fuzzer for a database wants
*narrow* value domains, the one question a generated query must be able to answer,
and where the whole approach stops.

Runs against the standard library only, `sqlite3` is in it, which is the quiet
reason this milestone was cheap. The generator itself lives under
`tests/differential/` and is not imported here: this is the argument, not the
harness.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Database
from engine.errors import ChenDBError
from engine.executor.engine import execute_script

WIDTH = 78


def rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * WIDTH}\n{title}\n{'─' * WIDTH}")
    else:
        print("─" * WIDTH)


def heading(text: str) -> None:
    print(f"\n{text}\n{'·' * len(text)}")


SETUP = (
    "CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, f FLOAT, s TEXT, b BOOLEAN);",
    "INSERT INTO t VALUES "
    "(1, 10, 1.5, 'a', TRUE), (2, NULL, 2.5, 'b', FALSE), (3, 30, -0.5, 'd', TRUE);",
)

SQLITE_SETUP = (
    "CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, f REAL, s TEXT, b BOOLEAN);",
    "INSERT INTO t VALUES "
    "(1, 10, 1.5, 'a', TRUE), (2, NULL, 2.5, 'b', FALSE), (3, 30, -0.5, 'd', TRUE);",
)


def ask_chendb(database: Database, sql: str) -> str:
    try:
        results = execute_script(sql, database)
    except ChenDBError as error:
        return f"{type(error).__name__}"
    return repr(results[-1].rows)


def ask_sqlite(sql: str) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in SQLITE_SETUP:
            connection.execute(statement)
        return repr(tuple(connection.execute(sql).fetchall()))
    except sqlite3.Error as error:
        return f"{type(error).__name__}"
    finally:
        connection.close()


def compare(database: Database, label: str, sql: str, was: str) -> None:
    mine, theirs = ask_chendb(database, sql), ask_sqlite(sql)
    print(f"  {label}")
    print(f"    {sql}")
    print(f"      before Milestone 17   {was}")
    print(f"      ChenDB now            {mine}")
    print(f"      SQLite                {theirs}")


def the_bugs(database: Database) -> None:
    rule("1. What a second engine found")
    print("""
Seven bugs, on the first real campaign. Not one would have failed an existing
test, because a test has to be aimed at something and these were the queries
nobody thought to write. Two were silent wrong answers.
""")

    heading("A non-boolean condition matched nothing, and said nothing")
    compare(
        database,
        "`WHERE v` over an INTEGER column",
        "SELECT COUNT(*) FROM t WHERE n;",
        "((0,),)   <- wrong, and no complaint",
    )
    print("""
    `Filter` asked `is_true(verdict)`, and `is_true` was `value is True`.
    `10 is True` is False, so a value that was never a condition looked
    exactly like a row that failed one. The same silence dropped every group
    from `HAVING SUM(v)`.
""")

    heading("SUM did whatever Python's + happened to do")
    compare(
        database,
        "`SUM` over TEXT",
        "SELECT SUM(s) FROM t;",
        "(('abd',),)   <- a string, as the sum of a column",
    )
    compare(
        database,
        "`AVG` over TEXT",
        "SELECT AVG(s) FROM t;",
        "TypeError     <- not even a ChenDBError; a 500, not a 400",
    )
    compare(
        database,
        "`SUM` over BOOLEAN",
        "SELECT SUM(b) FROM t;",
        "((2,),) typed BOOLEAN, but ((True,),) over one row",
    )

    heading("INTEGER meant 64 bits on disk and not in an expression")
    compare(
        database,
        "arithmetic past int64",
        "SELECT n + 9223372036854775807 FROM t WHERE id = 1;",
        "((9223372036854775817,),)   <- the engine refuses to *store* that",
    )


def a_test_that_agreed_with_the_bug() -> None:
    rule("2. A test existed, and it agreed")
    print("""
The non-boolean WHERE had a test pointed straight at it:

    assert is_true(1) is False, "a truthy non-boolean must not pass"

That comment is right. Python's truthiness must not leak into SQL, or
`WHERE name` would keep every row with a non-empty name. The assertion stopped
one step short of the rule it wanted.

Not passing and being *rejected* are the same thing to a filter. So the test
asserted the bug, and it will have read as careful work to every reviewer,
because it is careful work. It just answered a slightly easier question than
the one that mattered.

The rule it was reaching for: a value that is not a boolean is not a
condition at all. That is now an error, at bind time where the type is known
and at evaluation time where it is not.
""")


def narrow_domains() -> None:
    rule("3. A database fuzzer wants narrow domains, not wide ones")
    print("""
The instinct is to draw values from a wide range. For a database it is exactly
backwards. Watch what a domain does to the thing you are trying to test:
""")
    for name, values in (
        ("wide  (0 .. 2^62)", [4611686018427387904 - i * 7919 for i in range(8)]),
        ("narrow (-3 .. 7)", [-3, -1, 0, 1, 2, 3, 7, 2]),
    ):
        left = values[:4]
        right = values[4:]
        matches = sum(1 for a in left for b in right if a == b)
        groups = len(set(values))
        print(f"  {name:<20} join matches {matches:>2}   distinct group keys {groups}/8")

    print("""
With wide values nothing ever matches, every GROUP BY makes one group per row,
and a hundred thousand cases exercise one code path. Small domains make
duplicate keys, multi-row groups, empty groups and unmatched rows the *common*
case.

The schema follows the same logic. The child's foreign key is drawn from three
pools (keys the parent has, keys it does not, and NULL) so a single join has
matched rows, orphans and unknown-keyed rows at once. That is the difference
between exercising a join and merely calling one.
""")


def the_hard_question(database: Database) -> None:
    rule("4. The question every generated query must answer")
    print("""
Is its answer uniquely defined? If not, there is nothing to compare against.

`ORDER BY` over a non-unique key leaves tied rows in an unspecified order, and
both engines are entitled to their own. So the tester generates it anyway (it
is where a NULL-ordering bug lives) and compares the three things that *are*
defined: the sort-key sequence exactly, the rows as a multiset, and the rows
within each run of equal keys.

That third clause is the one that earns its keep. Without it, a sort that
carries a row across a tie boundary satisfies the other two and passes.
""")
    heading("And where NULLs sort is not a bug in either engine")
    sql = "SELECT id, n FROM t ORDER BY n;"
    print(f"    {sql}")
    print(f"      ChenDB   {ask_chendb(database, sql)}   <- NULLs last, like PostgreSQL")
    print(f"      SQLite   {ask_sqlite(sql)}   <- NULLs first")
    print("""
The standard leaves it open. So SQLite is *asked* for ChenDB's order (every
generated sort key carries NULLS LAST or NULLS FIRST on the SQLite side) rather
than the generator giving up on nullable sort keys, which would have discarded
the most interesting case in the suite.

The rule that keeps this honest: a translation is allowed only when the
difference is notation or representation, never when it is about what the query
*means*. A compatibility layer that quietly repairs a disagreement is worse than
no tester, it is a green tick over a bug.
""")


def primary_keys(database: Database) -> None:
    rule("5. The gap that was already written down")
    print("""
`PRIMARY KEY` was not enforced. Two rows with the same key, no complaint, and
it is recorded as a known limitation in *two* milestone documents.

What nobody had connected: unique indexes have existed and worked since
Milestone 5. The machinery was sitting there; nothing pointed the constraint at
it. Creating a table now creates `<table>_pkey`.
""")
    try:
        execute_script("INSERT INTO t VALUES (1, 0, 0.0, 'z', TRUE);", database)
        print("  a duplicate primary key was accepted   <- the old behaviour")
    except ChenDBError as error:
        print(f"  INSERT INTO t VALUES (1, ...)  ->  {type(error).__name__}: {error}")

    print("\n  And it is a real index, visible like any other:")
    for index in database.indexes():
        print(
            f"    {index.name:<12} on {index.table_name}.{index.column_name}"
            f"  unique={index.unique}"
        )
    print("""
    Deliberately visible. It costs real pages and shows up in the plan view, so
    hiding it would make a primary key look free and the page count unexplained.
""")


def main() -> int:
    print("ChenDB, Milestone 17: differential testing against SQLite")
    print(f"sqlite3 {sqlite3.sqlite_version}, from the standard library")

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "differential.chendb"
        with Database.open(path) as database:
            for statement in SETUP:
                execute_script(statement, database)

            the_bugs(database)
            a_test_that_agreed_with_the_bug()
            narrow_domains()
            the_hard_question(database)
            primary_keys(database)

    rule("Where it stops")
    print("""
- No outer joins in the generator. That is Milestone 18, and the schema was
  built for it, the orphans and NULL keys are already there.
- No DISTINCT, subqueries, CASE, IN, LIKE or CAST: the generator can only emit
  what ChenDB parses, so the narrower grammar bounds what can be compared.
- No concurrency and no crashes. The tester compares answers to one query at a
  time; MVCC and recovery have their own suites.
- Plans are compared only where a generated schema happens to have an index.
  "An index must not change the answer" was violated, and is checked by
  accident rather than on purpose.

  docs/milestone-17-differential.md has the seven bugs in full, and the five
  constraints that stop the divergence registry becoming a place to hide one.
""")
    rule()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
