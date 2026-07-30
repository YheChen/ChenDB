"""Transactions: making a group of writes all-or-nothing.

    BEGIN
      INSERT INTO users VALUES (1, 'ada')     page 4 changes  → before-image
      INSERT INTO orders VALUES (1, 1, 9.99)  page 9 changes  → before-image
      INSERT INTO users VALUES (1, 'dup')     fails
    ROLLBACK
      page 9 ← its before-image
      page 4 ← its before-image                 the database is as it was

Every page write in the engine funnels through ``Pager._write_at``, so that is
the single place a before-image is captured.  One hook covers the heap, every
index, both catalog tables and the meta page, with nothing in any of them
changed, which is why ``CREATE TABLE`` became atomic in this milestone without
the catalog knowing transactions exist.

Implicit and explicit
---------------------
A statement run with no transaction open gets one anyway, committed when it
finishes.  So ``INSERT INTO t VALUES (a), (b), (c)`` that fails on ``c`` leaves
none of them, which it did not before.

:func:`execute_script` wraps a whole script rather than each statement. That is
*not* what the SQL standard says (autocommit is per statement) and it is what
``execute_script``'s docstring has promised since Milestone 3: "a script that
fails half-way leaves the statements before the failure applied. Milestone 8
makes that atomic." A script is one unit of work here; a client that wants
per-statement autocommit sends one statement at a time.

``BEGIN`` inside an implicit transaction **adopts** it rather than nesting, so a
script reading ``BEGIN; …; COMMIT;`` behaves the way it looks. Nesting proper
needs savepoints, which ChenDB does not have.

The hard part: the buffer pool
------------------------------
Milestone 7's pool writes a dirty page to disk when it evicts one, and it has no
idea whether the transaction that dirtied it has committed. In ARIES vocabulary
that is a **steal** policy, and steal is what makes crash recovery need a log.

ChenDB allows steal, and the consequence is precise:

* **Rollback in this process is always correct**, evicted or not. The
  before-images are in memory; writing one back through the pool re-admits the
  page with the old bytes, whether the page was still resident or had been
  written out an hour ago.
* **A crash mid-transaction is not atomic.** Whatever the pool happened to
  evict is on disk, the undo log died with the process, and nothing on disk says
  a transaction was open.

The obvious fix (pin dirty uncommitted pages so they cannot be stolen) was
considered and rejected, because *it does not buy crash atomicity either*.
No-steal keeps uncommitted pages out of the file, but a crash **during the
commit flush** still leaves a partial transaction, and nothing on disk
distinguishes that from a complete one. Making commit atomic needs a commit
*record*: one small durable write that says "everything before this counts".
That is a write-ahead log, and it is Milestone 9. Pinning would have cost pool
exhaustion on large transactions in exchange for nothing.

So the boundary is stated rather than blurred: **atomic against errors, not
against power loss.** ``tests/recovery/`` pins that down in both directions.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from engine.concurrency.snapshot import (
    DEFAULT_ISOLATION,
    IsolationLevel,
    Snapshot,
)
from engine.diagnostics.events import (
    SnapshotEvent,
    TransactionEvent,
    UndoRecordEvent,
)
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import TransactionError
from engine.storage.pager import WriteIntent
from engine.transaction.undo import UndoLog

if TYPE_CHECKING:
    from engine.transaction.undo import UndoRecord

#: The session a caller gets when it does not name one. Everything written
#: before Milestone 10 uses this and behaves exactly as it did.
DEFAULT_SESSION: Final = "default"

__all__ = [
    "DEFAULT_SESSION",
    "Transaction",
    "TransactionManager",
    "TransactionState",
]


class TransactionState(StrEnum):
    """Where a transaction is. There is no PREPARED: no two-phase commit."""

    ACTIVE = "active"
    FAILED = "failed"
    """Open, but doomed. A statement raised inside it, so the only things that
    may follow are ``COMMIT`` (which rolls back) and ``ROLLBACK``.

    PostgreSQL has exactly this state and reports it as "current transaction is
    aborted, commands ignored until end of transaction block". Without it, a
    client that sent ``BEGIN``, hit an error, and then sent ``COMMIT`` would
    keep whatever ran before the failure, half a transaction, committed, which
    is the one outcome this milestone exists to prevent."""
    COMMITTED = "committed"
    ABORTED = "aborted"

    @property
    def is_open(self) -> bool:
        """ACTIVE or FAILED: still holding an undo log."""
        return self in (TransactionState.ACTIVE, TransactionState.FAILED)

    @property
    def is_finished(self) -> bool:
        return not self.is_open


@dataclass(slots=True)
class Transaction:
    """One unit of work, and everything needed to take it back."""

    transaction_id: int
    started_at_ns: int
    implicit: bool
    """True when the engine opened this, not the user. An implicit transaction
    commits when its statement or script ends; an explicit one waits for
    ``COMMIT``."""
    session: str = "default"
    """Which session owns it. One transaction per session at a time."""
    isolation: IsolationLevel = DEFAULT_ISOLATION
    snapshot: Snapshot | None = None
    """The view it reads through. Re-taken per statement under READ COMMITTED,
    kept for the transaction's life under REPEATABLE READ."""
    snapshots_taken: int = 0
    """How many times. Under REPEATABLE READ it is one; under READ COMMITTED it
    is the statement count, which is the difference made countable."""
    rows_created: int = 0
    rows_deleted: int = 0
    locks_held: int = 0
    undo: UndoLog = field(default_factory=UndoLog)
    state: TransactionState = TransactionState.ACTIVE
    statements: int = 0
    pages_written: int = 0
    pages_restored: int = 0
    """Set by a rollback. Kept on the transaction because ``undo`` is cleared
    as soon as it finishes, so ``pages_held`` reads zero afterwards."""
    finished_at_ns: int = 0

    @property
    def duration_ns(self) -> int:
        end = self.finished_at_ns or time.monotonic_ns()
        return end - self.started_at_ns

    @property
    def pages_held(self) -> int:
        return self.undo.page_count

    @property
    def undo_bytes(self) -> int:
        return self.undo.bytes_held

    def records(self) -> tuple[UndoRecord, ...]:
        return self.undo.records()


class TransactionManager:
    """Every open transaction, keyed by the session that opened it.

    Milestone 8 held exactly one and said so. Milestone 10 holds one *per
    session*, which is a smaller change than it sounds: the engine still runs
    one statement at a time, so nothing here has to arbitrate simultaneous
    access to a page. What is new is that a transaction can stay open across
    statements belonging to *other* sessions, and a lock it holds meanwhile is
    what blocks them.

    That is the honest shape of the concurrency this engine has. Two sessions
    genuinely interleave, genuinely conflict, and genuinely deadlock; they do
    not genuinely execute at the same instant. Claiming otherwise would mean
    making every structure below this thread-safe, which is a different project.

    Sessions are strings. A single-session caller never mentions one and gets
    :data:`DEFAULT_SESSION`, so every pre-Milestone-10 call site still reads the
    same and still means the same.
    """

    __slots__ = (
        "_by_session",
        "_frozen_xid",
        "_history",
        "_history_limit",
        "_lock",
        "_next_id",
        "_running",
        "_tracer",
    )

    #: Finished transactions kept for the timeline. Bounded, because a long
    #: session would otherwise accumulate every transaction it ever ran, the
    #: same reason the execution store is bounded.
    HISTORY_LIMIT = 50

    def __init__(self, *, tracer: Tracer | None = None, frozen_xid: int = 0) -> None:
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._lock = threading.RLock()
        #: The open transaction for each session that has one.
        self._by_session: dict[str, Transaction] = {}
        #: The same transactions, keyed by id, what a snapshot needs.
        self._running: dict[int, Transaction] = {}
        self._history: list[Transaction] = []
        self._history_limit = self.HISTORY_LIMIT
        self._next_id = max(frozen_xid, 1)
        self._frozen_xid = frozen_xid

    # -- state -------------------------------------------------------------

    @property
    def frozen_xid(self) -> int:
        """Ids below this committed before this process started.

        Read from the meta page at open. It is the entire commit log, see
        :mod:`engine.concurrency.snapshot` for why one number suffices here and
        does not in PostgreSQL.
        """
        return self._frozen_xid

    @frozen_xid.setter
    def frozen_xid(self, value: int) -> None:
        self._frozen_xid = value

    @property
    def next_xid(self) -> int:
        return self._next_id

    def running_ids(self) -> frozenset[int]:
        """Ids of every transaction currently open, in any session."""
        with self._lock:
            return frozenset(self._running)

    def running(self) -> tuple[Transaction, ...]:
        with self._lock:
            return tuple(self._running.values())

    def sessions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._by_session))

    def active_in(self, session: str = DEFAULT_SESSION) -> Transaction | None:
        with self._lock:
            return self._by_session.get(session)

    @property
    def active(self) -> Transaction | None:
        """The default session's transaction.

        Kept as a property so every call site written before sessions existed
        still means what it meant. A multi-session caller uses
        :meth:`active_in`.
        """
        return self.active_in()

    def in_transaction_in(self, session: str = DEFAULT_SESSION) -> bool:
        return self.active_in(session) is not None

    @property
    def in_transaction(self) -> bool:
        return self.active is not None

    @property
    def any_open(self) -> bool:
        """True when *any* session has a transaction open.

        Distinct from :attr:`in_transaction`, and the distinction matters:
        a checkpoint has to refuse while anybody's transaction is open, not
        just the default session's.
        """
        with self._lock:
            return bool(self._running)

    @property
    def in_explicit_transaction(self) -> bool:
        active = self.active
        return active is not None and not active.implicit

    def history(self) -> tuple[Transaction, ...]:
        """Finished transactions, most recent last."""
        with self._lock:
            return tuple(self._history)

    def all_transactions(self) -> tuple[Transaction, ...]:
        with self._lock:
            return (*self._history, *self._running.values())

    # -- lifecycle ---------------------------------------------------------

    def begin(
        self,
        *,
        implicit: bool = False,
        session: str = DEFAULT_SESSION,
        isolation: IsolationLevel = DEFAULT_ISOLATION,
    ) -> Transaction:
        """Open a transaction for ``session``. Raises if it already has one."""
        with self._lock:
            existing = self._by_session.get(session)
            if existing is not None:
                if implicit:
                    # An implicit transaction inside an existing one is a no-op:
                    # the outer one already covers the work.
                    return existing
                if not existing.implicit:
                    raise TransactionError(
                        f"session {session!r} already has a transaction open; "
                        f"ChenDB has no savepoints, so transactions do not nest"
                    )
                # BEGIN inside an implicit transaction adopts it, so a script
                # reading `BEGIN; …; COMMIT;` behaves the way it looks.
                existing.implicit = False
                self._emit(existing, "begin")
                return existing

            transaction = Transaction(
                transaction_id=self._next_id,
                started_at_ns=time.monotonic_ns(),
                implicit=implicit,
                session=session,
                isolation=isolation,
            )
            self._next_id += 1
            self._by_session[session] = transaction
            self._running[transaction.transaction_id] = transaction
            self._emit(transaction, "begin")
            return transaction

    # -- snapshots ---------------------------------------------------------

    def snapshot_for(self, transaction: Transaction) -> Snapshot:
        """The view this transaction should read through, right now.

        Under REPEATABLE READ the snapshot is taken once and kept; under READ
        COMMITTED a new one is taken per statement. **That is the only
        difference between the two levels**, and putting it in one branch here
        rather than spreading it through the read path is the point of having a
        snapshot object at all.
        """
        if transaction.snapshot is not None and not transaction.isolation.per_statement:
            return transaction.snapshot

        with self._lock:
            snapshot = Snapshot.take(
                next_xid=self._next_id,
                active=set(self._running),
                frozen_xid=self._frozen_xid,
                owner=transaction.transaction_id,
            )
        transaction.snapshot = snapshot
        transaction.snapshots_taken += 1
        if self._tracer.operator:
            self._tracer.emit(
                SnapshotEvent(
                    transaction_id=transaction.transaction_id,
                    isolation_level=transaction.isolation.value,
                    xmin=snapshot.xmin,
                    xmax=snapshot.xmax,
                    active_count=len(snapshot.active),
                )
            )
        return snapshot

    def oldest_snapshot_xmin(self) -> int:
        """The lowest ``xmin`` any open transaction could still be reading at.

        Vacuum's horizon: a version deleted by a transaction below this can no
        longer be wanted by anybody, so its space is reclaimable. A single
        long-running transaction holds this number down and stops vacuuming
        making progress, which is PostgreSQL's most common "why is my disk
        full" answer, and it is the same mechanism.
        """
        with self._lock:
            live = [t.snapshot.xmin for t in self._running.values() if t.snapshot]
            return min(live) if live else self._next_id

    def mark_failed(self, session: str = DEFAULT_SESSION) -> None:
        """A statement raised. Nothing further may be accepted.

        Called from the SQL layer, which is the one place a statement's failure
        is observable. The embedded API does not need it: ``with
        db.transaction():`` already rolls back when the block raises.
        """
        active = self.active_in(session)
        if active is not None and active.state is TransactionState.ACTIVE:
            active.state = TransactionState.FAILED
            self._emit(active, "failed")

    def is_failed_in(self, session: str = DEFAULT_SESSION) -> bool:
        active = self.active_in(session)
        return active is not None and active.state is TransactionState.FAILED

    @property
    def is_failed(self) -> bool:
        return self.is_failed_in()

    def commit(self, session: str = DEFAULT_SESSION) -> Transaction:
        """Accept the work. The undo log is discarded, not applied.

        Nothing is written here. The pages are already in the buffer pool, dirty
        or already stolen, and durability is still :meth:`Pager.sync`'s job, so
        a commit costs a state change and a freed undo log. That is *no-force*
        in ARIES terms, and it is only safe because a commit here does not claim
        to be durable. Milestone 9 is where commit means something on disk.
        """
        transaction = self._require_active("COMMIT", session)
        if transaction.state is TransactionState.FAILED:
            # The manager refuses; :meth:`Database.commit` catches this case
            # earlier and rolls back instead, which is what PostgreSQL does with
            # a COMMIT in an aborted block. Reaching here means a caller went
            # around the database, and accepting half a transaction would be
            # worse than raising.
            raise TransactionError(
                "cannot COMMIT: a statement in this transaction failed, so it "
                "can only be rolled back"
            )
        transaction.state = TransactionState.COMMITTED
        transaction.finished_at_ns = time.monotonic_ns()
        self._emit(transaction, "commit")
        transaction.undo.clear()
        self._retire(transaction)
        return transaction

    def rollback(
        self, apply: Callable[[int, bytes], None], session: str = DEFAULT_SESSION
    ) -> Transaction:
        """Undo the work by writing every before-image back, newest first.

        ``apply`` writes one page image; the manager does not know how. That
        keeps this module free of the pager and makes rollback testable against
        a dictionary.
        """
        transaction = self._require_active("ROLLBACK", session)
        self._emit(transaction, "rollback_started")

        restored = 0
        for record in transaction.undo.rewind():
            apply(record.page_id, record.before_image)
            restored += 1
            if self._tracer.verbose:
                self._tracer.emit(
                    UndoRecordEvent(
                        transaction_id=transaction.transaction_id,
                        page_id=record.page_id,
                        kind="restore",
                        before_image_size=record.size,
                        reason=record.reason,
                    )
                )

        transaction.state = TransactionState.ABORTED
        transaction.pages_restored = restored
        transaction.finished_at_ns = time.monotonic_ns()
        self._emit(transaction, "rollback_done", pages=restored)
        transaction.undo.clear()
        self._retire(transaction)
        return transaction

    def _require_active(self, what: str, session: str = DEFAULT_SESSION) -> Transaction:
        active = self.active_in(session)
        if active is None:
            raise TransactionError(f"{what} with no transaction open")
        return active

    def _retire(self, transaction: Transaction) -> None:
        with self._lock:
            self._by_session.pop(transaction.session, None)
            self._running.pop(transaction.transaction_id, None)
            self._history.append(transaction)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]

    # -- the write hook ----------------------------------------------------

    def before_write(
        self,
        page_id: int,
        current: Callable[[], bytes],
        reason: str = "",
        session: str = DEFAULT_SESSION,
    ) -> WriteIntent | None:
        """Capture ``page_id``'s current bytes, if a transaction needs them.

        ``current`` is a callable rather than the bytes themselves so the page
        is only read when a record is actually going to be kept, which, thanks
        to first-write-wins, is the minority of writes in any real transaction.

        Returns what the pager needs to log this change: which transaction it
        belongs to, and the before-image if this is that transaction's first
        write to the page. Both mechanisms (the in-memory undo log and the
        on-disk one) capture on exactly the same rule, so the page is read at
        most once either way.
        """
        transaction = self.active_in(session)
        if transaction is None:
            return None
        if transaction.undo.has(page_id):
            transaction.pages_written += 1
            return WriteIntent(transaction_id=transaction.transaction_id)

        transaction.pages_written += 1
        image = current()
        if transaction.undo.capture(page_id, image, reason) and self._tracer.storage:
            self._tracer.emit(
                UndoRecordEvent(
                    transaction_id=transaction.transaction_id,
                    page_id=page_id,
                    kind="capture",
                    before_image_size=len(image),
                    reason=reason,
                )
            )
        return WriteIntent(transaction_id=transaction.transaction_id, before_image=image)

    def note_statement(self, session: str = DEFAULT_SESSION) -> None:
        active = self.active_in(session)
        if active is not None:
            active.statements += 1

    # -- diagnostics -------------------------------------------------------

    def _emit(self, transaction: Transaction, action: str, pages: int = 0) -> None:
        if not self._tracer.summary:
            return
        self._tracer.emit(
            TransactionEvent(
                transaction_id=transaction.transaction_id,
                action=action,  # type: ignore[arg-type]
                implicit=transaction.implicit,
                pages_held=transaction.pages_held,
                undo_bytes=transaction.undo_bytes,
                pages_restored=pages,
            )
        )

    def __repr__(self) -> str:
        with self._lock:
            open_now = len(self._running)
        state = f"{open_now} open" if open_now else "idle"
        return f"<TransactionManager {state} history={len(self._history)}>"
