"""Who can see which version of a row.

    transaction 5 inserts row A          A.xmin = 5
    transaction 5 commits
    transaction 7 starts, takes a snapshot        ← sees A
    transaction 8 deletes row A          A.xmax = 8
    transaction 8 commits
    transaction 7 reads again                     ← STILL sees A

Transaction 7's snapshot was taken before 8 existed, so as far as 7 is
concerned 8 has not happened. The row is physically still there (a delete
writes eight bytes of header rather than removing anything) and that is the
entire trick: **a reader never waits for a writer, because it reads an older
version instead of the newer one.**

The cost is that dead versions accumulate and something has to remove them
later. PostgreSQL ships an autovacuum daemon for exactly this;
:meth:`Database.vacuum` is ChenDB's much smaller version of the same job.

The rule
--------
A version is visible to a snapshot when its creator has committed *and* its
deleter has not::

    visible(row, snapshot) =
            committed_before(row.xmin, snapshot)
        and not committed_before(row.xmax, snapshot)

with one exception for a transaction's own writes, which it must see even
though it has not committed: a transaction that could not read back what it
just inserted would be surprising in a way no isolation level asks for.

Why there is no commit log
--------------------------
PostgreSQL needs one (``pg_xact``, formerly CLOG) because it does **not
undo**. An aborted transaction's tuples stay in the heap with their ``xmin``
set, and the only way to know they are dead is to look the transaction up. That
lookup is also what makes transaction-id wraparound dangerous, and why
anti-wraparound VACUUM exists.

ChenDB rolls back by restoring pages, so an aborted transaction's rows are
*physically gone*. Every row that survives to be read was written by a
transaction that committed. So the entire commit log collapses into one number:

    xid < frozen_xid  →  committed, and its effects are final

:attr:`Snapshot.frozen_xid` comes from the meta page, set at each checkpoint,
which cannot run while a transaction is open, so at that moment every
transaction has finished. Only ids at or above it need looking up, and those
are the ones this process is running right now, in memory.

This is a simplification bought by a design decision three milestones ago, and
it is worth being clear that it is not free: undoing on abort is what makes
rollback cost time proportional to pages touched, where PostgreSQL's costs
nothing. The bill arrives somewhere either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from engine.serialization.record import NO_TRANSACTION_ID, TupleHeader

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

__all__ = ["IsolationLevel", "Snapshot", "visible"]


class IsolationLevel(StrEnum):
    """How often a transaction takes a new snapshot.

    That is the whole difference, and it is smaller than the names suggest.
    """

    READ_COMMITTED = "read committed"
    """A fresh snapshot for **every statement**. Two identical ``SELECT``s in
    one transaction can return different rows, because a concurrent transaction
    committed in between, a *non-repeatable read*. PostgreSQL's default, and
    the level most applications actually run at without knowing."""

    REPEATABLE_READ = "repeatable read"
    """One snapshot for the **whole transaction**, taken at its first read.
    Every statement sees the same database. This is snapshot isolation, and it
    is what PostgreSQL gives you when you ask for ``REPEATABLE READ``, stronger
    than the standard requires, because the standard permits phantom rows and
    snapshot isolation does not.

    It is *not* serializable. Two transactions can each read what the other is
    about to overwrite and both commit, producing a state no serial order could
, write skew. Ruling that out needs predicate locking or PostgreSQL's
    serializable snapshot isolation, and ChenDB has neither."""

    @property
    def per_statement(self) -> bool:
        return self is IsolationLevel.READ_COMMITTED


#: The level a transaction gets when nobody says otherwise. PostgreSQL's
#: default too, and for the same reason: repeatable read makes a long-running
#: transaction hold back vacuuming for as long as it lives.
DEFAULT_ISOLATION: IsolationLevel = IsolationLevel.READ_COMMITTED


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A view of the database as of one instant.

    Three numbers and a set, which is all PostgreSQL's ``SnapshotData`` is too.
    """

    xmin: int
    """The lowest transaction id still running when this was taken. Anything
    below it has finished."""
    xmax: int
    """One past the highest id assigned. Anything at or above it started *after*
    this snapshot and is invisible by definition."""
    active: frozenset[int]
    """Ids between :attr:`xmin` and :attr:`xmax` that were still running. The
    holes in the range. A transaction that started before this one and has not
    committed yet."""
    frozen_xid: int = 0
    """Ids below this are committed and final. See the module docstring."""
    owner: int = NO_TRANSACTION_ID
    """The transaction this snapshot belongs to, so it can see its own
    uncommitted writes."""

    @classmethod
    def take(
        cls,
        *,
        next_xid: int,
        active: AbstractSet[int],
        frozen_xid: int,
        owner: int = NO_TRANSACTION_ID,
    ) -> Snapshot:
        """Capture the running transactions right now.

        Cheap by construction (a set copy of however many transactions are
        open, which for one writer is at most one) and that matters because
        READ COMMITTED takes a new one per statement.
        """
        running = frozenset(active)
        return cls(
            xmin=min(running) if running else next_xid,
            xmax=next_xid,
            active=running,
            frozen_xid=frozen_xid,
            owner=owner,
        )

    def sees(self, xid: int) -> bool:
        """Was ``xid``'s work committed and visible when this was taken?"""
        if xid == NO_TRANSACTION_ID:
            return False
        if xid == self.owner:
            # A transaction always sees its own writes, committed or not.
            # Anything else would mean an INSERT you cannot then SELECT.
            return True
        if xid < self.frozen_xid:
            return True
        if xid >= self.xmax:
            return False  # started after this snapshot
        return xid not in self.active

    def describe(self) -> str:
        active = ",".join(str(x) for x in sorted(self.active)) or "none"
        return f"xmin={self.xmin} xmax={self.xmax} active={{{active}}}"


def visible(header: TupleHeader, snapshot: Snapshot) -> bool:
    """Should this version of the row be returned?

    Two questions, in this order, because the first is far more often the
    decisive one: most rows in a table were inserted long ago by a transaction
    that committed, and were never deleted.
    """
    if not snapshot.sees(header.xmin):
        return False
    return not snapshot.sees(header.xmax)


@dataclass(slots=True)
class VisibilityStats:
    """How much work the visibility check did, for the row inspector.

    ``skipped`` is the interesting one. It counts rows that are physically on
    the page and were not returned, dead versions the reader paid to walk past.
    A number that grows and never falls is what a missing vacuum looks like.
    """

    checked: int = 0
    returned: int = 0
    skipped_invisible: int = 0
    """Created by a transaction this snapshot cannot see: still running, or
    started later."""
    skipped_deleted: int = 0
    """Deleted by a transaction this snapshot *can* see. Dead, and reclaimable
    once no older snapshot exists."""

    dead_rows: int = field(default=0)
    """Versions that no snapshot could ever want again. What vacuum removes."""

    def note(self, header: TupleHeader, snapshot: Snapshot) -> bool:
        self.checked += 1
        if not snapshot.sees(header.xmin):
            self.skipped_invisible += 1
            return False
        if snapshot.sees(header.xmax):
            self.skipped_deleted += 1
            return False
        self.returned += 1
        return True
