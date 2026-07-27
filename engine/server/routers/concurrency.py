"""Lock table, sessions, and vacuum.

    GET  /databases/{db}/locks      who holds what, and who is waiting
    GET  /databases/{db}/sessions   every console's transaction and snapshot
    POST /databases/{db}/vacuum     reclaim versions nobody can want

Sessions are named in the **query string** of the endpoints that already exist
(``?session=alice``) rather than being resources of their own. A session here is
not a thing the server allocates and hands back — it is just a label saying
which transaction a statement belongs to, and inventing a create/destroy
lifecycle for a label would be ceremony with no state behind it.

The one thing this router owns is *reading* that state back, because a
two-console demonstration is unreadable without being able to see both sides.
"""

from __future__ import annotations

from fastapi import APIRouter

from engine.server import mappers
from engine.server.deps import DatabaseDep
from engine.server.schemas.concurrency import LockTableResponse, SessionListResponse
from engine.server.schemas.wal import CheckpointResponse

router = APIRouter(prefix="/databases/{database_id}", tags=["concurrency"])


@router.get(
    "/locks",
    response_model=LockTableResponse,
    summary="The lock table and the wait-for graph",
)
def get_locks(managed: DatabaseDep) -> LockTableResponse:
    """Writers only. A reader never appears here, which is the point."""
    with managed.use() as db:
        return mappers.locks_to_api(db.locks.snapshot())


@router.get(
    "/sessions",
    response_model=SessionListResponse,
    summary="Every session's transaction, snapshot and locks",
)
def get_sessions(managed: DatabaseDep) -> SessionListResponse:
    with managed.use() as db:
        return mappers.sessions_to_api(db.transactions, db.locks)


@router.post(
    "/vacuum",
    response_model=CheckpointResponse,
    summary="Reclaim row versions no snapshot can want",
)
def run_vacuum(managed: DatabaseDep) -> CheckpointResponse:
    """Manual, on purpose.

    A background daemon in a teaching engine would make row counts move on
    their own while somebody was reading them, and the *reason* space is not
    reclaimed at delete time is exactly what this milestone is trying to show.

    Reuses ``CheckpointResponse`` because it is answering the same question in
    the same units — how much space came back — and a second near-identical
    model would only mean two things to keep in step.
    """
    with managed.use() as db:
        before = db.page_count * db.page_size
        reclaimed = db.vacuum()
        after = db.page_count * db.page_size
        horizon = db.transactions.oldest_snapshot_xmin()

    return CheckpointResponse(
        pages_flushed=0,
        bytes_reclaimed=reclaimed,
        log_size_before=before,
        log_size_after=after,
        base_lsn=horizon,
        message=(
            f"{reclaimed} dead version(s) reclaimed below transaction {horizon}."
            if reclaimed
            else "Nothing to reclaim. Either nothing has been deleted, or an "
            "open transaction's snapshot might still want it."
        ),
    )
