"""ARIES recovery: analysis, redo, undo.

    log:  [u1 t7] [u2 t7] [u3 t9] [commit t7] [u4 t9] ✗ crash
                                                      │
    analysis   t7 committed → winner                  │
               t9 never did → loser                   │
                                                      ▼
    redo       replay u1 u2 u3 u4 — everything, losers included
    undo       walk t9's records backwards, restore before-images
               logging each restore, then abort t9

Three passes, in that order, and the middle one looks wrong the first time you
see it: **redo replays the losers too.** ARIES calls this *repeating history*,
and the reason is that recovery cannot know which of a loser's changes reached
the disk and which did not. Rather than reason about each page, it puts the
database into the exact state the crash left it in — every logged change
applied — and then rolls the losers back from there, using the same undo path a
live rollback uses. One mechanism, exercised constantly, instead of a second
one that only ever runs after a crash.

Idempotence
-----------
Recovery must survive being interrupted and run again, because a machine that
crashed once can crash again while recovering. Two things make that work:

* **Redo is conditional.** A record is applied only if the page's own LSN is
  below it. Applying an already-applied change is skipped, not repeated.
* **Undo is logged.** Each restore is appended as an ordinary ``UPDATE`` record
  before it is applied. ARIES calls these compensation log records; the effect
  is that a crash mid-undo leaves the completed part of the undo *in the log*,
  so the next recovery redoes it during its own redo pass and only undoes what
  is left. Without them, undo would restart from the beginning and — with
  before-images rather than deltas — would still be correct, but only by
  accident of this design rather than by construction.

What is not here
----------------
No dirty-page table and no transaction table are reconstructed during analysis.
Both exist in real ARIES so that redo can start at the earliest change that
might not be on disk, rather than at the checkpoint. ChenDB's checkpoints are
*sharp* — they flush everything — so the earliest such change is always the
first record after the checkpoint, and the log never contains anything that can
be skipped wholesale. That is the simplification a stop-the-world checkpoint
buys, and it is the reason this file is a page long instead of five.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from engine.diagnostics.events import RecoveryActionEvent, RecoveryPhaseEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.wal.log import WriteAheadLog
from engine.wal.record import NO_TRANSACTION, LogRecord, RecordType

__all__ = ["RecoveryReport", "recover"]


@dataclass(slots=True)
class RecoveryReport:
    """What recovery found and did. Empty on a clean open."""

    ran: bool = False
    """False when the log was empty — the usual case, and the fast path."""
    records_scanned: int = 0
    truncated_tail: bool = False
    """A record at the end of the log was incomplete. Expected after a crash:
    the process died part-way through a write."""
    winners: tuple[int, ...] = ()
    """Transactions that committed or aborted. Their work stands."""
    losers: tuple[int, ...] = ()
    """Transactions the crash caught in flight. Their work is undone."""
    pages_redone: int = 0
    pages_skipped: int = 0
    """Records whose change was already on the page. The higher this is
    relative to ``pages_redone``, the more work the last checkpoint saved."""
    pages_undone: int = 0
    highest_lsn: int = 0
    highest_xid: int = 0
    """The largest transaction id in the log. One past it is where new ids must
    start: reusing one would make an existing row's ``xmin`` ambiguous."""
    duration_ns: int = 0
    phase_ns: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """True when nothing had to be undone."""
        return not self.losers

    def summary(self) -> str:
        if not self.ran:
            return "clean shutdown; nothing to recover"
        return (
            f"recovered {self.records_scanned} record(s): "
            f"{self.pages_redone} redone, {self.pages_skipped} already current, "
            f"{self.pages_undone} undone; "
            f"{len(self.winners)} finished, {len(self.losers)} interrupted"
        )


def recover(
    log: WriteAheadLog,
    *,
    read_page_lsn: Callable[[int], int],
    apply_page: Callable[[int, bytes], None],
    tracer: Tracer | None = None,
) -> RecoveryReport:
    """Bring the database file up to date with the log.

    ``read_page_lsn`` answers "what LSN does page *n* carry on disk", returning
    **-1** for a page that is not there or cannot be read. Not 0: zero is a real
    LSN, the very first record in a stream, and conflating the two makes redo
    skip it.

    ``apply_page`` writes a page image. Both come from the pager, so this module
    never learns what a page is; that is the same boundary the undo log keeps,
    and it is why recovery can be tested against a dictionary.
    """
    tracer = tracer if tracer is not None else NULL_TRACER
    started = time.perf_counter_ns()
    report = RecoveryReport()

    records, truncated = log.read_all()
    report.truncated_tail = truncated
    report.records_scanned = len(records)
    if not records:
        return report

    report.ran = True
    winners, losers = _analyse(records, report, tracer)
    _redo(records, report, tracer, read_page_lsn, apply_page)
    _undo(records, losers, log, report, tracer, apply_page)

    report.winners = tuple(sorted(winners))
    report.losers = tuple(sorted(losers))
    report.duration_ns = time.perf_counter_ns() - started
    return report


# -- pass 1 ----------------------------------------------------------------


def _analyse(
    records: list[LogRecord], report: RecoveryReport, tracer: Tracer
) -> tuple[set[int], set[int]]:
    """Split the transactions into those that finished and those that did not.

    ``ABORT`` counts as finishing. A rolled-back transaction wrote its restores
    through the ordinary page path, so they are in the log as ``UPDATE``
    records; replaying them lands on the pre-transaction state, which is where
    the rollback already was. Undoing it *again* would be undoing the undo.
    """
    started = time.perf_counter_ns()
    _emit_phase(tracer, "analysis", "started")

    seen: set[int] = set()
    finished: set[int] = set()
    for record in records:
        if record.transaction_id != NO_TRANSACTION:
            seen.add(record.transaction_id)
            report.highest_xid = max(report.highest_xid, record.transaction_id)
        if record.record_type in (RecordType.COMMIT, RecordType.ABORT):
            finished.add(record.transaction_id)
        report.highest_lsn = max(report.highest_lsn, record.end_lsn)

    elapsed = time.perf_counter_ns() - started
    report.phase_ns["analysis"] = elapsed
    _emit_phase(tracer, "analysis", "finished", len(records), elapsed)
    return finished, seen - finished


# -- pass 2 ----------------------------------------------------------------


def _redo(
    records: list[LogRecord],
    report: RecoveryReport,
    tracer: Tracer,
    read_page_lsn: Callable[[int], int],
    apply_page: Callable[[int, bytes], None],
) -> None:
    """Replay every update whose page has not already got it.

    The LSN comparison is what makes this idempotent, and it is the only reason
    a page needs to carry an LSN at all: without it recovery could not tell a
    change that reached the disk from one that did not, and would have to
    replay the whole log every time — correct here, because whole-page images
    are idempotent anyway, but not correct for any real log format, and it
    would turn every recovery into a full rewrite of the database.
    """
    started = time.perf_counter_ns()
    _emit_phase(tracer, "redo", "started")

    for record in records:
        if record.record_type is not RecordType.UPDATE:
            continue
        page_lsn = read_page_lsn(record.page_id)
        if page_lsn >= record.lsn:
            report.pages_skipped += 1
            _emit_action(
                tracer,
                "redo",
                record,
                "skip",
                f"page lsn {page_lsn} >= record lsn {record.lsn}",
            )
            continue
        apply_page(record.page_id, record.after_image)
        report.pages_redone += 1
        _emit_action(
            tracer, "redo", record, "redo", f"page lsn {page_lsn} < record lsn {record.lsn}"
        )

    elapsed = time.perf_counter_ns() - started
    report.phase_ns["redo"] = elapsed
    _emit_phase(
        tracer, "redo", "finished", report.pages_redone + report.pages_skipped, elapsed
    )


# -- pass 3 ----------------------------------------------------------------


def _undo(
    records: list[LogRecord],
    losers: set[int],
    log: WriteAheadLog,
    report: RecoveryReport,
    tracer: Tracer,
    apply_page: Callable[[int, bytes], None],
) -> None:
    """Roll back everything the crash caught in flight, newest first.

    Only one record per page per transaction carries a before-image —
    first-write-wins, the same rule the in-memory undo log follows — so there is
    exactly one image to restore per page and no need to reason about which of
    several is the right one. Walking backwards is still what ARIES specifies
    and still what happens here, because a reader who sees it walk forwards will
    reasonably wonder what happens when there *are* several.
    """
    if not losers:
        report.phase_ns["undo"] = 0
        return

    started = time.perf_counter_ns()
    _emit_phase(tracer, "undo", "started")

    for record in reversed(records):
        if record.record_type is not RecordType.UPDATE:
            continue
        if record.transaction_id not in losers or not record.has_undo:
            continue
        # Log the compensation before applying it, so a crash here leaves the
        # completed part of the undo recoverable by the next redo pass.
        log.append(
            RecordType.UPDATE,
            transaction_id=record.transaction_id,
            page_id=record.page_id,
            after_image=record.before_image,
        )
        apply_page(record.page_id, record.before_image)
        report.pages_undone += 1
        _emit_action(
            tracer,
            "undo",
            record,
            "undo",
            f"transaction {record.transaction_id} never finished",
        )

    for transaction_id in sorted(losers):
        log.abort(transaction_id)
    log.flush(sync=True)

    elapsed = time.perf_counter_ns() - started
    report.phase_ns["undo"] = elapsed
    _emit_phase(tracer, "undo", "finished", report.pages_undone, elapsed)


# -- diagnostics -----------------------------------------------------------


def _emit_phase(
    tracer: Tracer, phase: str, action: str, records: int = 0, elapsed: int = 0
) -> None:
    if not tracer.summary:
        return
    tracer.emit(
        RecoveryPhaseEvent(
            phase=phase,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            records_processed=records,
            duration_ns=elapsed,
        )
    )


def _emit_action(
    tracer: Tracer, phase: str, record: LogRecord, decision: str, reason: str
) -> None:
    if not tracer.operator:
        return
    tracer.emit(
        RecoveryActionEvent(
            phase=phase,  # type: ignore[arg-type]
            lsn=record.lsn,
            page_id=record.page_id,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
        )
    )
