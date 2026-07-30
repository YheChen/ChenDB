"""Write-ahead log, checkpoint and recovery endpoints.

    GET  /databases/{db}/wal          the records, the LSNs, the fsync cost
    GET  /databases/{db}/recovery     what the last open had to repair
    POST /databases/{db}/checkpoint   flush the pages, discard the log
    POST /databases/{db}/crash        abandon the handle without flushing

The crash button
----------------
``POST /crash`` **destroys uncommitted work on purpose.** It drops the database
handle without flushing dirty pages, running a checkpoint, or rolling anything
back, and the next request reopens the file — which runs recovery.

It is here because the alternative is a recovery panel that describes recovery
instead of showing it, and a reader has no reason to believe a description. The
honesty of the demonstration is the whole value: committed rows are still there
afterwards *because their commit records were fsynced*, and uncommitted ones are
gone *because they were not*, and both are visible in the row counts this
endpoint returns from before and after.

It is scoped to one database in a workspace the server was pointed at, it
deletes no files, and it cannot lose anything the engine promised to keep. What
it can lose is exactly what a power cut would lose, which is the point.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from engine.server import mappers
from engine.server.deps import DatabaseDep, WorkspaceDep
from engine.server.schemas.wal import (
    CheckpointResponse,
    CrashResponse,
    RecoveryReportModel,
    WalResponse,
)

router = APIRouter(prefix="/databases/{database_id}", tags=["wal"])

#: Records returned by default. A busy log holds thousands and the panel shows
#: a window; ``total_records`` says how many there really are, so the view can
#: say "showing the last 200 of 12,000" rather than implying it has them all.
DEFAULT_RECORD_LIMIT = 200
MAX_RECORD_LIMIT = 2000


@router.get(
    "/wal",
    response_model=WalResponse,
    summary="The log: records, LSNs, and what fsync costs",
)
def get_wal(
    managed: DatabaseDep,
    limit: Annotated[int, Query(ge=1, le=MAX_RECORD_LIMIT)] = DEFAULT_RECORD_LIMIT,
) -> WalResponse:
    with managed.use() as db:
        log = db.pager.wal
        size = log.path.stat().st_size if log is not None else 0
        return mappers.wal_to_api(log, limit=limit, size_bytes=size)


@router.get(
    "/recovery",
    response_model=RecoveryReportModel,
    summary="What the last open had to repair",
)
def get_recovery(managed: DatabaseDep) -> RecoveryReportModel:
    """Empty after a clean shutdown, which is what makes it meaningful.

    A clean close ends with a checkpoint and leaves an empty log, so ``ran``
    says exactly "the previous process did not shut down properly".
    """
    with managed.use() as db:
        return mappers.recovery_to_api(db.pager.recovery)


@router.post(
    "/checkpoint",
    response_model=CheckpointResponse,
    summary="Flush every dirty page, then discard the log",
)
def run_checkpoint(managed: DatabaseDep) -> CheckpointResponse:
    """Refused while a transaction is open — see :meth:`Database.checkpoint`."""
    with managed.use() as db:
        log = db.pager.wal
        before = log.path.stat().st_size if log is not None else 0
        reclaimed_before = log.stats.bytes_reclaimed if log is not None else 0

        pages = db.checkpoint()

        after = log.path.stat().st_size if log is not None else 0
        reclaimed = (log.stats.bytes_reclaimed - reclaimed_before) if log is not None else 0
        base = log.base_lsn if log is not None else 0

    return CheckpointResponse(
        pages_flushed=pages,
        bytes_reclaimed=reclaimed,
        log_size_before=before,
        log_size_after=after,
        base_lsn=base,
        message=(
            f"{pages} page(s) flushed; {reclaimed:,} bytes of log discarded. "
            f"Everything before LSN {base:,} is now on the pages themselves."
        ),
    )


@router.post(
    "/crash",
    response_model=CrashResponse,
    summary="Simulate a crash: destroys uncommitted work, then recovers",
)
def simulate_crash(managed: DatabaseDep, workspace: WorkspaceDep) -> CrashResponse:
    """Abandon the handle without flushing, reopen, and report what survived.

    The row counts are gathered on both sides of the crash by this endpoint
    rather than left to the caller, because a caller that forgot to ask first
    would have nothing to compare against — and "some rows are gone" is not a
    demonstration without the number they were.
    """
    with managed.use() as db:
        before = _row_counts(db)

    workspace.crash(managed.database_id)

    # Reopening is what runs recovery. Reporting the pre-crash state here and
    # letting a later poll show the truth would make the response a lie.
    reopened = workspace.get(managed.database_id)
    with reopened.use() as db:
        report = mappers.recovery_to_api(db.pager.recovery)
        after = _row_counts(db)

    lost = sum(before.values()) - sum(after.values())
    return CrashResponse(
        message=(
            f"handle abandoned without flushing; recovery {report.summary}. "
            + (
                f"{lost:,} uncommitted row(s) did not survive."
                if lost > 0
                else "Nothing uncommitted was outstanding, so nothing was lost."
            )
        ),
        recovered=report,
        rows_before=before,
        rows_after=after,
    )


def _row_counts(db) -> dict[str, int]:
    """Rows per user table. Counted, not estimated — the planner's statistics
    would be a guess, and a guess is not evidence of anything."""
    return {table.name: db.count(table.name) for table in db.catalog.list_tables()}
