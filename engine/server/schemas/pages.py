"""Page-inspector API models.

These mirror the frozen dataclasses in :mod:`engine.storage.inspect` one for
one.  The duplication is the point: the engine's shapes stay free to change
with the storage format, and the wire contract stays stable for the frontend.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from engine.server.schemas.common import ApiModel

__all__ = [
    "FieldLayoutModel",
    "HeaderFieldModel",
    "PageDetailModel",
    "PageListResponse",
    "PageSummaryModel",
    "RecordLayoutModel",
    "SlotDetailModel",
]


class HeaderFieldModel(ApiModel):
    """One decoded header field, with the bytes it was read from."""

    name: str
    offset: int
    size: int
    value: int | str
    raw_hex: str
    description: str


class PageSummaryModel(ApiModel):
    page_id: int
    page_type: str = Field(description="META, HEAP, SCHEMA, FREE, ...")
    file_offset: int = Field(description="page_id * page_size")
    lsn: int = Field(description="Always 0 until Milestone 9 adds the WAL")
    checksum: int
    checksum_valid: bool
    slot_count: int = Field(description="Directory entries, tombstones included")
    live_record_count: int
    free_space: int = Field(description="Contiguous bytes between the two regions")
    reclaimable_space: int = Field(description="Bytes compaction would recover")
    next_page_id: int | None
    owner: str = Field(description="Table name, or 'meta' / 'schema' / 'unallocated'")
    error: str | None = None
    dirty: bool = Field(
        default=False,
        description=(
            "Always false in Milestone 1: without a buffer pool every write "
            "goes straight through, so no page is ever cached-and-dirty."
        ),
    )


class FieldLayoutModel(ApiModel):
    index: int
    name: str
    type_name: str
    is_null: bool
    offset: int = Field(description="-1 for NULL, which occupies no bytes")
    length: int
    value: Any


class RecordLayoutModel(ApiModel):
    values: list[Any]
    fields: list[FieldLayoutModel]
    null_bitmap_hex: str
    null_bitmap_bits: list[bool] = Field(
        description="One entry per column; true means NULL"
    )
    null_bitmap_size: int
    total_size: int


class SlotDetailModel(ApiModel):
    slot_id: int
    offset: int
    length: int
    is_live: bool
    raw_hex: str
    record: RecordLayoutModel | None = None
    decode_error: str | None = None
    xmin: int = Field(
        default=0,
        description="The transaction that created this version. Zero for a "
        "catalog row, which is not versioned.",
    )
    xmax: int = Field(
        default=0,
        description="The transaction that deleted it, or 0. Non-zero on a slot "
        "that is still live means a **dead version**: physically there, "
        "invisible to anyone new, and waiting for a vacuum.",
    )


class PageDetailModel(ApiModel):
    summary: PageSummaryModel
    header_fields: list[HeaderFieldModel]
    slots: list[SlotDetailModel]
    raw_hex: str = Field(description="The entire page, hex-encoded")
    page_size: int
    header_size: int
    slot_directory_end: int
    free_start: int
    free_end: int


class PageListResponse(ApiModel):
    pages: list[PageSummaryModel]
    page_size: int
    page_count: int
    total_bytes: int
