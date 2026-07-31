#!/usr/bin/env python3
"""A narrated tour of Milestone 19: proving an outer join is an inner join.

    python examples/milestone19_outer_join_simplification.py

Milestone 18 ran an outer join exactly where it was written and never asked
whether it had to be one. Often it does not. `a LEFT JOIN b ON … WHERE b.x = 5`
keeps every `a` row that found no partner, fills `b`'s columns with NULLs, and
then throws every one of those rows away, because `NULL = 5` is NULL and a WHERE
keeps only TRUE.

Five things: the proof, the four shapes it produces, the two rewrites it unblocks,
the shapes it must never touch, and what it costs to be sure it is right.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.executor.binder import Scope, ScopeEntry, bind_expression
from engine.executor.engine import execute_script
from engine.optimizer.nullability import rejects_nulls, truth_of
from engine.parser.parser import parse_statement
from engine.planner.physical import PhysicalJoin, walk_physical

WIDTH = 78

STAFF = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
    Column("team", DataType.TEXT),
)
SHIFTS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("staff_id", DataType.INTEGER),
    Column("hours", DataType.INTEGER),
)
NOTES = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("shift_id", DataType.INTEGER),
    Column("body", DataType.TEXT),
)

STAFF_ROWS = [
    (1, "ada", "core"),
    (2, "alan", "core"),
    (3, "grace", "ops"),
    (4, "edsger", "ops"),
]
SHIFT_ROWS = [
    (10, 1, 8),
    (11, 1, 4),
    (12, 2, 9),
    (13, 3, 2),
    (14, 99, 6),  # an orphan: nobody has id 99
    (15, None, 7),  # a NULL key, which matches nothing, not even another NULL
]
NOTE_ROWS = [(20, 10, "late"), (21, 13, "swap"), (22, 99, "orphan")]


def rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * WIDTH}\n{title}\n{'─' * WIDTH}")
    else:
        print("─" * WIDTH)


def heading(text: str) -> None:
    print(f"\n{text}\n{'·' * len(text)}")


def run(database: Database, sql: str):
    return execute_script(sql, database)[-1]


def kinds(database: Database, sql: str) -> str:
    """What each join in the chosen plan actually preserves, innermost first."""
    names = []
    for node in reversed(walk_physical(run(database, sql).planned.root)):
        if isinstance(node, PhysicalJoin):
            names.append(node.outer_label or "INNER")
    return " then ".join(names) or "no join"


def fired(database: Database, sql: str) -> bool:
    return "simplify_outer_joins" in run(database, sql).planned.rewrites


# The scope the analysis runs against in part 1: `s` at position 0, `sh` at
# position 1, the same shape the planner hands it.
SCOPE = Scope(
    (
        ScopeEntry("s", "staff", STAFF, offset=0, position=0),
        ScopeEntry("sh", "shifts", SHIFTS, offset=len(STAFF), position=1),
    )
)


def the_proof() -> None:
    rule("1. The proof: what can this predicate possibly say about an invented row?")
    print("""
An outer join preserves an unmatched row by filling the other side with NULLs.
So ask a smaller question than "what does this predicate evaluate to", which
would need a row: what is the *set* of results it could produce, given that every
column of `sh` is NULL and everything else is unknown?

Push that through SQL's own truth tables. If TRUE is not in the answer, no
preserved row can survive the WHERE, and preserving them was work with no
observer. The join was an inner join written the long way.
""")
    nulled = frozenset({1})
    for sql in (
        "sh.hours > 5",
        "sh.hours IS NULL",
        "sh.hours IS NOT NULL",
        "sh.hours > 5 AND s.team = 'core'",
        "sh.hours > 5 OR s.team = 'core'",
        "NOT (sh.hours = 5)",
        "s.team = 'core'",
    ):
        statement = parse_statement(f"SELECT * FROM staff WHERE {sql}")
        predicate = bind_expression(statement.where, SCOPE)
        possible = ", ".join(sorted(item.name for item in truth_of(predicate, nulled)))
        verdict = "rejects" if rejects_nulls([predicate], nulled) else "  ...  "
        print(f"  {verdict}  {sql:<34} could be {{{possible}}}")
    print("""
    Read the two OR rows against each other. `TRUE OR unknown` is TRUE, so one
    survivable branch is enough to keep a preserved row alive; `FALSE AND
    unknown` is FALSE, so one rejecting branch is enough to kill it.

    And read the IS NULL row twice. It is the only predicate here that is TRUE
    about a row the join invented, which is exactly why the anti-join idiom
    works and exactly why rewriting it would be a disaster.
""")


def four_shapes(database: Database) -> None:
    rule("2. A join is two booleans, not four names")
    print("""
LEFT preserves the rows written to its left, RIGHT those to its right, FULL both,
INNER neither. So the rewrite is one line applied twice, and FULL is the case
that makes that worth saying: it can lose one side and still be an outer join.
""")
    print(f"  {'staff FULL JOIN shifts …':<46} {'runs as':<8} why")
    for where, note in (
        ("", "nothing to prove"),
        ("WHERE sh.hours > 5", "the invented shifts die"),
        ("WHERE s.team = 'ops'", "the invented staff die"),
        ("WHERE s.team = 'ops' AND sh.hours > 5", "both"),
        ("WHERE sh.hours IS NULL", "TRUE about the invented rows"),
    ):
        sql = (
            f"SELECT s.name, sh.id FROM staff s FULL JOIN shifts sh "
            f"ON s.id = sh.staff_id {where};"
        )
        label = where or "(no WHERE)"
        print(f"  {label:<46} {kinds(database, sql):<8} {note}")


def what_it_unblocks(database: Database) -> None:
    rule("3. The rule saves nothing by itself. It saves what it unblocks")
    print("""
An inner join and an outer join over the same inputs cost the same to run. The
rewrite removes no operator. What it removes is two prohibitions Milestone 18 had
to impose, both correct, both expensive.
""")

    heading("Pushdown, which an outer join may not cross into the preserved side")
    for kind, where in (("LEFT", "sh.hours > 5"), ("LEFT", "sh.hours IS NULL")):
        sql = (
            f"SELECT s.name FROM staff s {kind} JOIN shifts sh "
            f"ON s.id = sh.staff_id WHERE {where};"
        )
        text = run(database, f"EXPLAIN {sql}").rows
        lines = [row[0] for row in text]
        below = next(
            (i for i, line in enumerate(lines) if "Filter" in line), len(lines)
        ) > next(i for i, line in enumerate(lines) if "Join" in line)
        print(
            f"  WHERE {where:<22} join is {kinds(database, sql):<8} "
            f"filter runs {'below (fewer rows joined)' if below else 'above the join'}"
        )

    heading("Reordering, which an outer join is a barrier to")
    for where in ("", "WHERE n.body = 'late'"):
        sql = (
            "SELECT s.name FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id "
            f"LEFT JOIN notes n ON sh.id = n.shift_id {where};"
        )
        lines = [row[0] for row in run(database, f"EXPLAIN {sql}").rows]
        barred = any("may not reorder across it" in line for line in lines)
        print(
            f"  {where or '(no WHERE)':<24} {kinds(database, sql):<26} "
            f"{'search is barred' if barred else 'search is free'}"
        )
    print("""
    Two LEFT joins the search may not touch become two inner joins it may order
    however it likes. That is the whole return on this milestone, and it arrives
    through parts of the planner this rule never mentions.
""")


def what_it_must_not_touch(database: Database) -> None:
    rule("4. The half that matters: where it must not fire")
    print("""
A rule that fires too eagerly here does not crash. It returns fewer rows, quietly,
and nothing downstream would attribute the loss to the optimiser.
""")
    cases = (
        ("sh.id IS NULL", "the anti-join idiom: TRUE about exactly the invented rows"),
        ("sh.hours > 5 OR s.team = 'ops'", "one survivable branch is enough"),
        ("s.team = 'ops'", "says nothing about the NULL-supplied side"),
    )
    for where, why in cases:
        sql = (
            "SELECT s.name FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id "
            f"WHERE {where};"
        )
        count = len(run(database, sql).rows)
        print(
            f"  WHERE {where:<32} {kinds(database, sql):<8} "
            f"{count} row{'' if count == 1 else 's':<2} {why}"
        )

    heading("And the ON of the outer join itself is not evidence about itself")
    print("""
`ON sh.hours > 5` cannot be TRUE about an invented row either, and it is still
not a reason to rewrite. An outer join's ON decides which rows *match*, and the
rows that do not match are precisely the ones it preserves. Treating the ON like
a WHERE is the entire difference between the two clauses.
""")
    sql = (
        "SELECT s.name, sh.id FROM staff s LEFT JOIN shifts sh "
        "ON s.id = sh.staff_id AND sh.hours > 5 ORDER BY s.name;"
    )
    result = run(database, sql)
    rendered = " ".join(
        f"{name}/{shift if shift is not None else 'NULL'}" for name, shift in result.rows
    )
    print(f"  plan: {kinds(database, sql)}   rows: {rendered}")


def evidence(database: Database) -> None:
    rule("5. What counts as evidence, and what it took to be sure")
    print("""
The WHERE is the obvious witness. It is not the only one. Any *later* join in the
chain that does not preserve its left input discards a row its ON rejects, and an
inner join is exactly such a join. So this collapses with no WHERE at all:
""")
    sql = (
        "SELECT s.name FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id "
        "JOIN notes n ON sh.id = n.shift_id;"
    )
    print("  SELECT s.name FROM staff s")
    print("    LEFT JOIN shifts sh ON s.id = sh.staff_id")
    print("         JOIN notes  n  ON sh.id = n.shift_id;")
    print(f"    fired={fired(database, sql)}   plan: {kinds(database, sql)}")
    print("""
    `sh.id = n.shift_id` is NULL for every row the LEFT join invented, and the
    inner join above drops it. Swap that JOIN for a LEFT JOIN and the evidence
    disappears, because a LEFT join preserves those rows instead of rejecting
    them:
""")
    sql = (
        "SELECT s.name FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id "
        "LEFT JOIN notes n ON sh.id = n.shift_id;"
    )
    print(f"    fired={fired(database, sql)}   plan: {kinds(database, sql)}")
    print("""
Being sure is the expensive part, and it is not a hand-written list:

  * an exhaustive soundness property in tests/unit/test_null_rejection.py: every
    predicate it can build, against every assignment of values, with the answer
    computed by the engine's real evaluator rather than a second opinion
  * the rewrite is run against itself with the rule switched off, over queries it
    fires on and queries it declines, and every answer has to be identical
  * 320,000 generated query pairs compared against SQLite, with the anti-join
    idiom drawn on purpose rather than hoped for
""")


def main() -> int:
    rule("Milestone 19: outer-join simplification")
    print("""
`a LEFT JOIN b ON … WHERE b.x = 5` is an inner join. Proving it is a small
analysis; the payoff is that two much larger parts of the planner stop being
forbidden to look at the query.
""")

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shifts.chendb"
        with Database.open(path, page_size=4096) as database:
            database.create_table("staff", STAFF)
            database.create_table("shifts", SHIFTS)
            database.create_table("notes", NOTES)
            database.insert_many("staff", STAFF_ROWS)
            database.insert_many("shifts", SHIFT_ROWS)
            database.insert_many("notes", NOTE_ROWS)
            run(database, "ANALYZE;")

            the_proof()
            four_shapes(database)
            what_it_unblocks(database)
            what_it_must_not_touch(database)
            evidence(database)

    rule("Where it stops")
    print("""
- Still no reordering *across* an outer join that survives the rewrite. This
  milestone removes the barrier when it can prove the join away; it does not make
  the barrier permeable. PostgreSQL's min_lefthand/min_righthand per outer join is
  how that is recovered, and it is a milestone of its own.
- HAVING is not evidence. It runs after grouping, and a NULL that survives into a
  group is a longer argument than this rule makes. Declining costs a rewrite, not
  an answer.
- One pass, outermost join inward. That is enough for a reduced join to count as
  evidence for the ones inside it, and not enough to reach a fixed point in every
  conceivable chain.
- No CASE, no COALESCE and no functions in the analysis, because the engine has
  none. Anything unrecognised comes back as "could be anything", which declines
  the rewrite rather than guessing at it.

  docs/milestone-19-outer-join-simplification.md has the analysis in full.
""")
    rule()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
