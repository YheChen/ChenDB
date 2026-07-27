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
changed — which is why ``CREATE TABLE`` became atomic in this milestone without
the catalog knowing transactions exist.

Implicit and explicit
---------------------
A statement run with no transaction open gets one anyway, committed when it
finishes.  So ``INSERT INTO t VALUES (a), (b), (c)`` that fails on ``c`` leaves
none of them, which it did not before.

:func:`execute_script` wraps a whole script rather than each statement. That is
*not* what the SQL standard says — autocommit is per statement — and it is what
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

The obvious fix — pin dirty uncommitted pages so they cannot be stolen — was
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

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from engine.diagnostics.events import TransactionEvent, UndoRecordEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import TransactionError
from engine.transaction.undo import UndoLog

if TYPE_CHECKING:
    from engine.transaction.undo import UndoRecord

__all__ = [
    "Transaction",
    "TransactionManager",
    "TransactionState",
]


class TransactionState(StrEnum):
    """Where a transaction is. There is no PREPARED: no two-phase commit."""

    ACTIVE = "active"
    FAILED = "failed"
    """Open, but doomed. A statement raised inside it, so the only things that
    may follow are ``COMMIT`` — which rolls back — and ``ROLLBACK``.

    PostgreSQL has exactly this state and reports it as "current transaction is
    aborted, commands ignored until end of transaction block". Without it, a
    client that sent ``BEGIN``, hit an error, and then sent ``COMMIT`` would
    keep whatever ran before the failure — half a transaction, committed, which
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
    """Owns the one open transaction, and the history of the finished ones.

    One at a time. The database-level write lock already serialises callers, so
    there is nothing for a concurrency-control scheme to arbitrate — that is
    Milestone 10's problem, and inventing a lock manager for a single writer
    would be structure with no user.
    """

    __slots__ = ("_active", "_history", "_history_limit", "_next_id", "_tracer")

    #: Finished transactions kept for the timeline. Bounded, because a long
    #: session would otherwise accumulate every transaction it ever ran — the
    #: same reason the execution store is bounded.
    HISTORY_LIMIT = 50

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._active: Transaction | None = None
        self._history: list[Transaction] = []
        self._history_limit = self.HISTORY_LIMIT
        self._next_id = 1

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> Transaction | None:
        return self._active

    @property
    def in_transaction(self) -> bool:
        return self._active is not None

    @property
    def in_explicit_transaction(self) -> bool:
        return self._active is not None and not self._active.implicit

    def history(self) -> tuple[Transaction, ...]:
        """Finished transactions, most recent last."""
        return tuple(self._history)

    def all_transactions(self) -> tuple[Transaction, ...]:
        active = (self._active,) if self._active else ()
        return (*self._history, *active)

    # -- lifecycle ---------------------------------------------------------

    def begin(self, *, implicit: bool = False) -> Transaction:
        """Open a transaction. Raises if one is already open and explicit."""
        if self._active is not None:
            if implicit:
                # An implicit transaction inside an existing one is a no-op:
                # the outer one already covers the work.
                return self._active
            if not self._active.implicit:
                raise TransactionError(
                    "a transaction is already open; ChenDB has no savepoints, "
                    "so transactions do not nest"
                )
            # BEGIN inside an implicit transaction adopts it, so a script
            # reading `BEGIN; …; COMMIT;` behaves the way it looks.
            self._active.implicit = False
            self._emit(self._active, "begin")
            return self._active

        transaction = Transaction(
            transaction_id=self._next_id,
            started_at_ns=time.monotonic_ns(),
            implicit=implicit,
        )
        self._next_id += 1
        self._active = transaction
        self._emit(transaction, "begin")
        return transaction

    def mark_failed(self) -> None:
        """A statement raised. Nothing further may be accepted.

        Called from the SQL layer, which is the one place a statement's failure
        is observable. The embedded API does not need it: ``with
        db.transaction():`` already rolls back when the block raises.
        """
        if self._active is not None and self._active.state is TransactionState.ACTIVE:
            self._active.state = TransactionState.FAILED
            self._emit(self._active, "failed")

    @property
    def is_failed(self) -> bool:
        return self._active is not None and self._active.state is TransactionState.FAILED

    def commit(self) -> Transaction:
        """Accept the work. The undo log is discarded, not applied.

        Nothing is written here. The pages are already in the buffer pool, dirty
        or already stolen, and durability is still :meth:`Pager.sync`'s job — so
        a commit costs a state change and a freed undo log. That is *no-force*
        in ARIES terms, and it is only safe because a commit here does not claim
        to be durable. Milestone 9 is where commit means something on disk.
        """
        transaction = self._require_active("COMMIT")
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

    def rollback(self, apply: Callable[[int, bytes], None]) -> Transaction:
        """Undo the work by writing every before-image back, newest first.

        ``apply`` writes one page image; the manager does not know how. That
        keeps this module free of the pager and makes rollback testable against
        a dictionary.
        """
        transaction = self._require_active("ROLLBACK")
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

    def _require_active(self, what: str) -> Transaction:
        if self._active is None:
            raise TransactionError(f"{what} with no transaction open")
        return self._active

    def _retire(self, transaction: Transaction) -> None:
        self._active = None
        self._history.append(transaction)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]

    # -- the write hook ----------------------------------------------------

    def before_write(
        self, page_id: int, current: Callable[[], bytes], reason: str = ""
    ) -> None:
        """Capture ``page_id``'s current bytes, if a transaction needs them.

        ``current`` is a callable rather than the bytes themselves so the page
        is only read when a record is actually going to be kept — which, thanks
        to first-write-wins, is the minority of writes in any real transaction.
        """
        transaction = self._active
        if transaction is None:
            return
        if transaction.undo.has(page_id):
            transaction.pages_written += 1
            return

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

    def note_statement(self) -> None:
        if self._active is not None:
            self._active.statements += 1

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
        state = f"active #{self._active.transaction_id}" if self._active else "idle"
        return f"<TransactionManager {state} history={len(self._history)}>"
