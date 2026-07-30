#!/usr/bin/env python3
"""A narrated tour of Milestone 18: outer joins, and what the planner gave up.

    python examples/milestone18_outer_joins.py

Five things: what an outer join keeps, why NULL-extending a row turned out to
cost nothing, why `ON` and `WHERE` stopped meaning the same thing, why an outer
join is a barrier the join-order search may not cross, and the bug a fuzzer found
that nine hand-written cases missed.

The parser refused these by name for five milestones, with a message that was
right about the reason: an outer join constrains the order the planner may join
in, and ChenDB reorders freely.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.executor.engine import execute_script
from engine.planner.physical import PhysicalJoin, walk_physical

WIDTH = 78

STAFF = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("name", DataType.TEXT, nullable=False),
)
SHIFTS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("staff_id", DataType.INTEGER),
    Column("hours", DataType.INTEGER),
)

STAFF_ROWS = [(1, "ada"), (2, "alan"), (3, "grace"), (4, "edsger")]
SHIFT_ROWS = [
    (10, 1, 8),
    (11, 1, 4),
    (12, 2, 9),
    (13, 3, 2),
    (14, 99, 6),  # an orphan: nobody has id 99
    (15, None, 7),  # a NULL key, which matches nothing, not even another NULL
]


def rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * WIDTH}\n{title}\n{'─' * WIDTH}")
    else:
        print("─" * WIDTH)


def heading(text: str) -> None:
    print(f"\n{text}\n{'·' * len(text)}")


def run(database: Database, sql: str):
    return execute_script(sql, database)[-1]


def show(database: Database, sql: str, note: str = "") -> None:
    result = run(database, sql)
    print(f"  {sql}")
    if note:
        print(f"    {note}")
    for row in result.rows:
        rendered = ", ".join("NULL" if value is None else repr(value) for value in row)
        print(f"      ({rendered})")


def what_it_keeps(database: Database) -> None:
    rule("1. An outer join keeps what an inner one throws away")
    print("""
Four staff, six shifts. Shift 14 belongs to nobody, shift 15 has no staff_id at
all, and edsger has never worked a shift. An inner join loses all three.
""")
    for kind in ("JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"):
        result = run(
            database,
            f"SELECT s.name, sh.id FROM staff s {kind} shifts sh "
            f"ON s.id = sh.staff_id ORDER BY s.name, sh.id;",
        )
        kept = [
            f"{name or 'NULL'}/{shift if shift is not None else 'NULL'}"
            for name, shift in result.rows
        ]
        print(f"  {kind:<11} {len(result.rows)} rows   {' '.join(kept)}")
    print("""
    LEFT adds edsger/NULL. RIGHT adds NULL/14 and NULL/15. FULL adds all three.
    None of the matched rows change.
""")

    heading("The anti-join idiom: the row you want is the one that did not match")
    show(
        database,
        "SELECT s.name FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id "
        "WHERE sh.id IS NULL;",
        "who has never worked, only askable because the join preserved them",
    )


def extension_is_free(database: Database) -> None:
    rule("2. NULL-extending a row turned out to cost nothing")
    print("""
Milestone 13 decided that a row's layout is the written order of the FROM,
always, so every row below the topmost join is the full width of the query,
with the tables not yet joined left as None. It paid for that in row width and
said so. This is the refund:

    staff ⟕ shifts,  a staff row with no partner

    [ 4, 'edsger', None, None, None ]
      └── staff ──┘ └── shifts ────┘
                     never written

The join copies the right side's columns into the left row by slice. A left row
that found no partner has simply never had that copy done to it, so emitting it
unchanged *is* the NULL extension. The whole of it.
""")
    (row,) = run(
        database,
        "SELECT s.id, s.name, sh.id, sh.staff_id, sh.hours FROM staff s "
        "LEFT JOIN shifts sh ON s.id = sh.staff_id WHERE s.id = 4;",
    ).rows
    print(
        f"    and every column of the missing side is NULL, not just the key:\n      {row}"
    )


def on_is_not_where(database: Database) -> None:
    rule("3. `ON` and `WHERE` stopped meaning the same thing")
    print("""
Milestone 13 put the WHERE and every join's ON into one pool of conjuncts, and
its comment said why: for an inner join the two are interchangeable. Every word
of that is false for an outer join.
""")
    show(
        database,
        "SELECT s.name, sh.hours FROM staff s LEFT JOIN shifts sh "
        "ON s.id = sh.staff_id AND sh.hours > 5 ORDER BY s.name, sh.hours;",
        "ON: decides what *matches*. Everyone survives; grace's 2-hour shift"
        " does not count, so she is NULL-extended.",
    )
    show(
        database,
        "SELECT s.name, sh.hours FROM staff s LEFT JOIN shifts sh "
        "ON s.id = sh.staff_id WHERE sh.hours > 5 ORDER BY s.name, sh.hours;",
        "WHERE: runs after extension. NULL > 5 is NULL, not TRUE, so it removes"
        " every preserved row and the outer join collapses to an inner one.",
    )
    print("""
    Both are correct. They are different queries, and the planner has to keep
    them apart, which is why an outer join's ON never enters the pool.
""")

    heading("And the plans show it")
    for sql in (
        "SELECT * FROM staff s JOIN shifts sh ON s.id = sh.staff_id AND sh.hours > 5;",
        "SELECT * FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id AND sh.hours > 5;",
        "SELECT * FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id WHERE sh.hours > 5;",
    ):
        print(f"  {sql}")
        for row in run(database, f"EXPLAIN {sql}").rows:
            line = row[0]
            if not line.strip() or line.startswith(("Statistics", "Decided", "Rewrites")):
                continue
            print(f"      {line}")
        print()
    print("""    Inner: the condition is pushed BELOW the join, a rewrite that can
    never be worse. Outer with it in the ON: it stays AT the join. Outer with it
    in the WHERE: it stays ABOVE. Three placements, three different queries.
""")


def a_barrier(database: Database) -> None:
    rule("4. An outer join is a barrier the search may not cross")
    print("""
The System R dynamic programme enumerates every left-deep order over a set of
relations, and its licence to do that is that an inner join is commutative and
associative. An outer join is neither: a ⟕ b and b ⟕ a are different queries.

So the chain is walked left to right. Consecutive inner joins accumulate into a
*segment* the search may order freely. An outer join closes the segment, runs
where it was written, and its result becomes ONE OPAQUE RELATION that the next
segment sees as a single input.

That is exactly the right amount of freedom, and it falls out rather than being
enforced. The search CAN commute that relation with others, swapping an inner
join's two inputs is sound. It CANNOT re-associate into it, because there is
nothing inside to reach: it can never turn (a ⟕ b) ⨝ c into a ⟕ (b ⨝ c).
""")
    for sql in (
        "SELECT * FROM staff s JOIN shifts sh ON s.id = sh.staff_id;",
        "SELECT * FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id;",
    ):
        decisions = [
            row[0]
            for row in run(database, f"EXPLAIN {sql}").rows
            if row[0].startswith("Decided what order")
        ]
        print(f"  {sql}")
        for line in decisions:
            print(f"      {line}")
    print("""
    What this gives up, said out loud rather than hidden: an inner join written
    after an outer one cannot move before it, even where that would be legal.
    PostgreSQL recovers those orderings with a min_lefthand/min_righthand
    relation set per outer join, so it can *prove* a reordering safe instead of
    assuming it is not. That is a milestone of its own.
""")

    heading("The build side is still chosen by cost, though")
    print("""
    preserve_left and preserve_right describe the *physical* inputs, not the
    LEFT or RIGHT in the query. Swapping an outer join's two inputs and flipping
    the flags gives the identical output row (because the row layout fixes every
    column's position by written order) so the cost model keeps its freedom to
    hash the smaller side. Only the order relative to other joins is constrained.
""")
    planned = run(
        database, "SELECT * FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id;"
    ).planned
    for node in walk_physical(planned.root):
        if isinstance(node, PhysicalJoin):
            print(f"      {node.node_type}: {node.detail}")
            print(
                f"      preserve_left={node.preserve_left} preserve_right={node.preserve_right}"
            )


def the_bug(database: Database) -> None:
    rule("5. The bug nine hand-written cases missed")
    print("""
A hash join emits an unmatched probe row when its bucket is EMPTY. That is the
obvious condition, and it is not the right one:

    FULL JOIN shifts sh ON s.id = sh.staff_id AND sh.hours > 100

A hash join hashes the equality and re-checks the rest per pair. So a probe row
can hash into a full bucket and be rejected by the residual for every candidate
in it. Its bucket was not empty; it matched nothing. Rows went missing.

I had tested FULL JOIN by hand, nine ways, and every one of my cases had a bare
equality for its ON. Milestone 17's generative suite found this on its first run
with outer joins enabled, because a third of the outer joins it emits carry an
extra term on the null-supplied side. I put that in the generator because it was
the shape the *planner* had to get right. It caught the executor.
""")
    result = run(
        database,
        "SELECT s.name, sh.id FROM staff s FULL JOIN shifts sh "
        "ON s.id = sh.staff_id AND sh.hours > 100 ORDER BY s.name, sh.id;",
    )
    print("    nothing can match, so a FULL join returns every row of both sides:")
    print(
        f"      {len(result.rows)} rows = {len(STAFF_ROWS)} staff + {len(SHIFT_ROWS)} shifts"
    )
    assert len(result.rows) == len(STAFF_ROWS) + len(SHIFT_ROWS)
    print("""
    "Is the bucket empty" and "did this row match" are different questions, and
    only the second one is the definition of unmatched.
""")


def what_it_costs(database: Database) -> None:
    rule("What it costs")
    for label, sql in (
        ("inner", "SELECT COUNT(*) FROM staff s JOIN shifts sh ON s.id = sh.staff_id;"),
        ("left", "SELECT COUNT(*) FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id;"),
        (
            "right",
            "SELECT COUNT(*) FROM staff s RIGHT JOIN shifts sh ON s.id = sh.staff_id;",
        ),
        ("full", "SELECT COUNT(*) FROM staff s FULL JOIN shifts sh ON s.id = sh.staff_id;"),
    ):
        best = min(_timed(database, sql) for _ in range(9))
        rows_out = run(database, sql).rows[0][0]
        print(f"  {label:<7} {best / 1000:7.1f} µs   {rows_out} rows")
    print("""
  Preserving the probe side is one flag test per probe row. Preserving the build
  side needs the buckets to hold *indices* into a list of build rows rather than
  the rows themselves, plus a set of which ones matched and a pass over the
  leftovers, one integer per build row over the old layout.

  The matched set is set[int] and not set[Row], which is not a micro-decision:
  two identical build rows are two rows, and a set of row values would treat
  them as one, so an unmatched duplicate would go missing.
""")


def _timed(database: Database, sql: str) -> int:
    start = time.perf_counter_ns()
    run(database, sql)
    return time.perf_counter_ns() - start


def main() -> int:
    print("ChenDB, Milestone 18: outer joins")

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "outer.chendb"
        with Database.open(path, page_size=4096) as database:
            database.create_table("staff", STAFF)
            database.create_table("shifts", SHIFTS)
            database.insert_many("staff", STAFF_ROWS)
            database.insert_many("shifts", SHIFT_ROWS)
            run(database, "ANALYZE;")

            what_it_keeps(database)
            extension_is_free(database)
            on_is_not_where(database)
            a_barrier(database)
            the_bug(database)
            what_it_costs(database)

    rule("Where it stops")
    print("""
- No reordering across an outer join, as above.
- No outer-join simplification. `a LEFT JOIN b ON … WHERE b.x = 5` is provably an
  inner join, because a null-rejecting WHERE on the null-supplied side discards
  every row the join preserved. Every serious planner spots that and rewrites it,
  which removes the barrier *and* re-enables pushdown. ChenDB executes the outer
  join and then filters, correct, and slower than it needs to be.
- No USING and no NATURAL JOIN. Both are sugar over ON, and both need the binder
  to merge two columns into one output column, which the flat row layout has no
  way to express.
- FULL JOIN always materialises. So does LEFT on the build side: a preserved side
  cannot be emitted until its input is known to be exhausted.
- The cardinality estimate is crude. The floor is right and the cross-product bug
  is gone, but join_selectivity has no notion of a foreign key, so a join along
  one is estimated like a join between unrelated columns.

  docs/milestone-18-outer-joins.md has the planner design in full.
""")
    rule()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
