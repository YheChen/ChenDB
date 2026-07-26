"""Buffer pool endpoints.

    GET /databases/{db}/buffer-pool   the frame grid, plus hit and miss counters

One endpoint, because a cache has one thing to say: what is in it and how often
that helped. The frame list includes free frames, so the grid has a fixed shape
and a page appearing in a slot is visible as a change rather than as a reflow.

The snapshot is taken under the engine lock and serialised outside it, like
every other diagnostics view — a browser polling the pool must never be able to
stall a query.
"""

from __future__ import annotations

from fastapi import APIRouter

from engine.server import mappers
from engine.server.deps import DatabaseDep
from engine.server.schemas.buffer import BufferPoolResponse

router = APIRouter(prefix="/databases/{database_id}", tags=["buffer-pool"])


@router.get(
    "/buffer-pool",
    response_model=BufferPoolResponse,
    summary="Every frame, and how often the cache helped",
)
def get_buffer_pool(managed: DatabaseDep) -> BufferPoolResponse:
    with managed.use() as db:
        snapshot = db.pager.buffer_pool.snapshot()
        pager_stats = db.stats
    return mappers.buffer_pool_to_api(snapshot, pager_stats)
