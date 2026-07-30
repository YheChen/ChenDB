"""Differences that are not bugs, written down as rules.

Two engines that disagree about *anything* need somewhere to record the
disagreements that are legitimate, or the suite is red forever and gets turned
off. That register is also the obvious place to hide a bug, so the interesting
part of this module is the constraints on what may go in it. Each is enforced by a
test in ``test_registry.py`` rather than by anyone's discipline:

1. **An entry may excuse an error, never a value.** Exactly one side must have
   erred. There is deliberately nowhere to record "both engines returned rows and
   the rows differed". That is precisely what the tester exists to find, and no
   wrong row is fine.

2. **A defensible difference in a *value* is fixed in the SQL, not in the
   comparison.** Either it is notation, and the dialect layer translates it
   (``NULLS LAST``), or the generator stops emitting the construct (float ``%``).
   Both are visible in the SQL a human reads when a case fails. Registering it
   would bury it inside the oracle, where nobody looks.

3. **Entries match on rules, not on identity.** :meth:`Entry.matches` is given
   only the two outcomes. An error class, and optionally a pattern over the
   message. It cannot see the SQL, the seed, the shape or a case id, so **there is
   no "known failing seeds" list**. That is the whole line: a divergence you can
   state as a rule over outcomes is knowledge, and a seed you excuse is a bug you
   have not diagnosed. The honest way to park an undiagnosed one is a named,
   minimal ``@pytest.mark.xfail(strict=True)`` test, which shows up every run and
   goes red the day it starts passing.

4. **One error class per entry.** An entry that would accept two must be split, so
   its reason has to be true of one thing.

5. **The list is capped.** Rules 1-4 keep each entry honest; a cap keeps the
   *list* honest, because a register that only ever grows is an escape hatch
   however carefully each line is worded. Hitting it forces a conversation rather
   than an append.

And it cannot rot: every entry carries a canonical minimal example, and a test
runs it and asserts the divergence **still happens**. When ChenDB stops raising on
division by zero, that test goes red and the entry has to go. The mechanism the
project's stale CLI milestone string never had.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tests.differential.engines import Outcome

__all__ = ["ENTRIES", "MAXIMUM_ENTRIES", "Entry", "Kind", "find"]

#: See constraint 5. Not a technical limit, a forcing function.
MAXIMUM_ENTRIES: Final = 20


class Kind(StrEnum):
    DIVERGENCE = "divergence"
    """The oracle accepts this pair of outcomes."""
    RESTRICTION = "restriction"
    """The generator refuses to emit this construct, so the oracle never sees it.
    Recorded here anyway: a restriction is a *coverage* decision, and an
    undocumented one silently narrows what the suite claims to test."""


@dataclass(frozen=True, slots=True)
class Entry:
    rule: str
    kind: Kind
    classification: str
    """``DELIBERATE`` or ``HARNESS``. Never ``BUG``. There is no such value, by
    construction, so a bug cannot be filed here instead of being fixed."""
    reason: str
    """Must say what PostgreSQL does. ChenDB follows PostgreSQL where it and
    SQLite differ, so an entry that does not say is one nobody has thought about."""
    setup: str
    sql: str
    chendb: str
    """``ok`` or ``err:<ErrorClass>``."""
    sqlite: str
    message_pattern: str = ""

    def matches(self, mine: Outcome, theirs: Outcome) -> bool:
        return self._side_matches(self.chendb, mine) and self._side_matches(
            self.sqlite, theirs
        )

    def _side_matches(self, expected: str, outcome: Outcome) -> bool:
        if expected == "ok":
            return outcome.ok
        wanted = expected.removeprefix("err:")
        if outcome.ok or outcome.error_class != wanted:
            return False
        if self.message_pattern:
            return re.search(self.message_pattern, outcome.error_message) is not None
        return True

    @property
    def error_classes(self) -> tuple[str, ...]:
        return tuple(
            side.removeprefix("err:") for side in (self.chendb, self.sqlite) if side != "ok"
        )


_ONE_ROW: Final = "CREATE TABLE d (k INTEGER PRIMARY KEY);\nINSERT INTO d VALUES (1);"

ENTRIES: Final[tuple[Entry, ...]] = (
    Entry(
        rule="division-by-zero-raises",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason=(
            "SQLite returns NULL for x/0. ChenDB raises, which is what PostgreSQL "
            "does. A NULL here would hide an arithmetic mistake in the query "
            "rather than report it, and the aggregate above would then silently "
            "skip the row."
        ),
        setup=_ONE_ROW,
        sql="SELECT 1 / 0 AS c0 FROM d;",
        chendb="err:EvaluationError",
        sqlite="ok",
        message_pattern="division by zero",
    ),
    Entry(
        rule="modulo-by-zero-raises",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason="As division by zero. PostgreSQL raises; SQLite returns NULL.",
        setup=_ONE_ROW,
        sql="SELECT 1 % 0 AS c0 FROM d;",
        chendb="err:EvaluationError",
        sqlite="ok",
        message_pattern="modulo by zero",
    ),
    Entry(
        rule="text-compared-with-a-number-refused",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason=(
            "SQLite's type affinity makes 'a' = 1 false and 10 < '9' true, by "
            "ordering NULL < numbers < text. ChenDB has static column types and "
            "refuses the comparison, as PostgreSQL does: silently choosing either "
            "answer for 10 < '9' is worse than asking for a cast."
        ),
        setup="CREATE TABLE t (k INTEGER PRIMARY KEY, s TEXT);\nINSERT INTO t VALUES (1, 'a');",
        sql="SELECT k AS c0 FROM t WHERE s = 1;",
        chendb="err:EvaluationError",
        sqlite="ok",
        message_pattern="cannot compare",
    ),
    Entry(
        rule="non-boolean-condition-refused",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason=(
            "SQLite coerces any value to a truth value, so WHERE v keeps every row "
            "with a non-zero v. ChenDB refuses, as PostgreSQL does. Worth its own "
            "note: until Milestone 17 ChenDB *silently matched nothing* here, "
            "because 5 is not True and a filter cannot tell a rejected row from a "
            "value that was never a condition. That was a bug; the fix turned it "
            "into an error, which is a difference. The same construct changed "
            "class, which is why an entry records its reason and not just its shape."
        ),
        setup="CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER);\nINSERT INTO t VALUES (1, 5);",
        sql="SELECT k AS c0 FROM t WHERE v;",
        chendb="err:BindingError",
        sqlite="ok",
        message_pattern="must be a boolean",
    ),
    Entry(
        rule="integer-overflow-raises",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason=(
            "SQLite promotes an overflowing integer expression to a float, changing "
            "the type of the answer to avoid an error. ChenDB raises, as PostgreSQL "
            "does, so INTEGER means exactly int64 wherever the value came from, "
            "the storage codec has always enforced that and the evaluator did not."
        ),
        setup=_ONE_ROW,
        sql="SELECT 9223372036854775807 + 1 AS c0 FROM d;",
        chendb="err:EvaluationError",
        sqlite="ok",
        message_pattern="overflowed",
    ),
    Entry(
        rule="sum-and-avg-need-a-number",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason=(
            "SQLite coerces text to a number for SUM, giving 0 for non-numeric "
            "text. PostgreSQL has no sum(text) and refuses; so does ChenDB. Before "
            "Milestone 17 it returned 'abd' (Python's + concatenating strings "
            "inside the accumulator) and AVG leaked a raw TypeError out of the "
            "engine, which was not even a ChenDBError."
        ),
        setup="CREATE TABLE t (k INTEGER PRIMARY KEY, s TEXT);\nINSERT INTO t VALUES (1, 'a');",
        sql="SELECT SUM(s) AS c0 FROM t;",
        chendb="err:BindingError",
        sqlite="ok",
        message_pattern="needs a number",
    ),
    Entry(
        rule="a-bare-column-beside-group-by-is-refused",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason=(
            "SQLite invents a value by picking an arbitrary row of the group. "
            "ChenDB refuses, as PostgreSQL and the standard require: a group is "
            "many rows and they do not agree on it. Never generated either, but "
            "recorded because it is the single largest semantic gap between the two."
        ),
        setup="CREATE TABLE t (k INTEGER PRIMARY KEY, g INTEGER, v INTEGER);\nINSERT INTO t VALUES (1, 1, 5), (2, 1, 6);",
        sql="SELECT v AS c0, COUNT(*) AS c1 FROM t GROUP BY g;",
        chendb="err:BindingError",
        sqlite="ok",
        message_pattern="must appear in GROUP BY",
    ),
    Entry(
        rule="null-in-an-integer-primary-key-refused",
        kind=Kind.DIVERGENCE,
        classification="DELIBERATE",
        reason=(
            "In SQLite an INTEGER PRIMARY KEY is the rowid, so a NULL is replaced "
            "by the next free one. PRIMARY KEY implies NOT NULL in the standard and "
            "in PostgreSQL, and ChenDB enforces that. Never generated (keys are "
            "numbered) but recorded because it is why they are numbered."
        ),
        setup="CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER);",
        sql="INSERT INTO t VALUES (NULL, 1);",
        chendb="err:NullConstraintViolation",
        sqlite="ok",
    ),
    Entry(
        rule="null-ordering-follows-postgres",
        kind=Kind.RESTRICTION,
        classification="HARNESS",
        reason=(
            "ChenDB sorts NULLs last on ASC and first on DESC, PostgreSQL's "
            "default, and the opposite of SQLite, which treats NULL as smaller "
            "than everything. The standard leaves it implementation-defined. Fixed "
            "in the SQL rather than in the oracle: every generated sort key carries "
            "NULLS LAST / NULLS FIRST on the SQLite side, so the two are asked the "
            "same question and ORDER BY over a nullable column stays testable."
        ),
        setup="CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER);\nINSERT INTO t VALUES (1, NULL), (2, 1);",
        sql="SELECT v AS c0 FROM t ORDER BY c0;",
        chendb="ok",
        sqlite="ok",
    ),
    Entry(
        rule="float-typed-modulo",
        kind=Kind.RESTRICTION,
        classification="DELIBERATE",
        reason=(
            "SQLite's % casts both operands to integers first, so 7.5 % 2 is 1.0 "
            "there and 1.5 here, which is PostgreSQL's answer. A difference in what "
            "the operator *means*, so the generator does not emit a float modulo, "
            "visible in the SQL, rather than hidden in a comparison that looks away."
        ),
        setup=_ONE_ROW,
        sql="SELECT 7.5 % 2 AS c0 FROM d;",
        chendb="ok",
        sqlite="ok",
    ),
    Entry(
        rule="boolean-is-a-real-type",
        kind=Kind.RESTRICTION,
        classification="HARNESS",
        reason=(
            "ChenDB has a BOOLEAN type with a one-byte codec; SQLite has none and "
            "stores 0/1, as does PostgreSQL's wire format for some clients. Purely "
            "representational, so it is normalised, bool to int, never the other "
            "way, because every bool is a 0 or a 1 but 2 is not a bool."
        ),
        setup="CREATE TABLE t (k INTEGER PRIMARY KEY, b BOOLEAN);\nINSERT INTO t VALUES (1, TRUE);",
        sql="SELECT MIN(b) AS c0 FROM t;",
        chendb="ok",
        sqlite="ok",
    ),
)


def find(mine: Outcome, theirs: Outcome) -> Entry | None:
    """The entry excusing this pair of outcomes, if any.

    Only ``DIVERGENCE`` entries can excuse anything. A ``RESTRICTION`` documents
    something the generator does not emit, so if one ever matched at runtime the
    generator has drifted, and silently accepting it would hide that.
    """
    for entry in ENTRIES:
        if entry.kind is Kind.DIVERGENCE and entry.matches(mine, theirs):
            return entry
    return None
