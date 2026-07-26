"""Buffer pool API models.

The frame grid is the point. A cache is one of the few parts of a database whose
behaviour is genuinely *visual* — you can watch a working set settle in, and
watch a scan wipe it out — and that only works if the API reports every frame,
including the empty ones.
"""

from __future__ import annotations

from pydantic import Field

from engine.server.schemas.common import ApiModel

__all__ = [
    "BufferPoolResponse",
    "BufferPoolStatsModel",
    "FrameModel",
]


class FrameModel(ApiModel):
    """One slot of the pool.

    There is no ``pin_count``. ChenDB's pool copies out of a frame rather than
    lending it, so nothing can be holding one when it is reused and a pin count
    would always read zero — a number that never prevents anything. See
    ``engine/storage/buffer.py`` for when that stops being true.
    """

    frame_id: int
    page_id: int | None = Field(description="Null when the frame is free")
    dirty: bool = Field(
        description="Written since it was loaded, so the disk copy is stale"
    )
    reads: int = Field(description="Times this page was served while resident")
    writes: int = Field(description="Times this page was written while resident")
    recency: int = Field(
        description="0 is the most recently used, and the last to be evicted. "
        "-1 for a free frame."
    )
    resident_for_ns: int


class BufferPoolStatsModel(ApiModel):
    hits: int
    misses: int
    lookups: int
    hit_rate: float = Field(description="Hits over lookups, 0..1")
    evictions: int
    dirty_evictions: int = Field(
        description="Evictions that had to write the frame back first"
    )
    writes_absorbed: int = Field(
        description="Logical writes that never reached the disk because the page "
        "was already dirty in a frame. The write-back win, counted directly."
    )
    flushes: int
    pages_flushed: int


class BufferPoolResponse(ApiModel):
    capacity: int = Field(description="Frames in the pool")
    page_size: int
    resident: int
    dirty: int
    bytes_used: int = Field(description="resident * page_size")
    frames: list[FrameModel]
    stats: BufferPoolStatsModel
    logical_reads: int = Field(
        description="Times the engine asked for a page, cumulative for this handle"
    )
    physical_reads: int = Field(description="…and how many became a syscall")
    logical_writes: int
    physical_writes: int
