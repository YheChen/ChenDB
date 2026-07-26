"""Read-only introspection of the storage engine.

Everything here produces plain frozen dataclasses describing *real* engine
state read from the actual file.  The visualizer's page inspector renders these
directly; nothing about the layout is reconstructed or simulated in the
frontend.

This module lives in the engine, not the server, for two reasons: the CLI needs
the same views, and keeping the shapes here means the server's job is a
mechanical dataclass-to-Pydantic mapping with no logic to get wrong.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from engine.errors import ChenDBError
from engine.serialization.record import RecordLayout, describe_record
from engine.serialization.schema import Schema
from engine.storage.constants import INVALID_PAGE_ID, META_PAGE_ID, PageType
from engine.storage.meta import META_HEADER_SIZE, MetaPage
from engine.storage.page import PAGE_HEADER_SIZE, SLOT_SIZE, Page
from engine.storage.pager import Pager

__all__ = [
    "HeaderField",
    "PageDetail",
    "PageSummary",
    "SlotDetail",
    "hexdump",
    "inspect_page",
    "render_page_map",
    "summarize_page",
]

_HEX_BYTES_PER_LINE: Final = 16


@dataclass(frozen=True, slots=True)
class HeaderField:
    """One decoded field of a page header, with the bytes it came from.

    Showing the field name, its byte range and its raw hex side by side is the
    point of the inspector: it makes the on-disk format legible rather than
    something you have to take on faith.
    """

    name: str
    offset: int
    size: int
    value: int | str
    raw_hex: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class PageSummary:
    """Enough about a page to render it in a list."""

    page_id: int
    page_type: str
    file_offset: int
    lsn: int
    checksum: int
    checksum_valid: bool
    slot_count: int
    live_record_count: int
    free_space: int
    reclaimable_space: int
    next_page_id: int | None
    owner: str
    """Which structure the page belongs to: a table name, ``meta``, ``schema``
    or ``free``. Milestone 5 adds index names here."""
    error: str | None = None
    """Set when the page could not be decoded; the rest is best-effort."""


@dataclass(frozen=True, slots=True)
class SlotDetail:
    """One slot-directory entry plus the record it points at."""

    slot_id: int
    offset: int
    length: int
    is_live: bool
    raw_hex: str
    record: RecordLayout | None = None
    decode_error: str | None = None


@dataclass(frozen=True, slots=True)
class PageDetail:
    """Everything the inspector shows for one page."""

    summary: PageSummary
    header_fields: tuple[HeaderField, ...]
    slots: tuple[SlotDetail, ...]
    raw: bytes
    page_size: int
    header_size: int
    slot_directory_end: int
    free_start: int
    free_end: int


def hexdump(
    data: bytes,
    *,
    start_offset: int = 0,
    width: int = _HEX_BYTES_PER_LINE,
    limit: int | None = None,
) -> str:
    """Classic ``offset  hex  |ascii|`` dump, for the CLI and tests."""
    view = data[:limit] if limit is not None else data
    lines: list[str] = []
    for base in range(0, len(view), width):
        chunk = view[base : base + width]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk).ljust(width * 3 - 1)
        ascii_part = "".join(
            chr(byte) if 0x20 <= byte < 0x7F else "." for byte in chunk
        )
        lines.append(f"{start_offset + base:08x}  {hex_part}  |{ascii_part}|")
    if limit is not None and len(data) > limit:
        lines.append(f"... {len(data) - limit} more bytes")
    return "\n".join(lines)


def _owner_for(page_id: int, owners: Mapping[int, str]) -> str:
    """Which structure a page belongs to.

    ``owners`` maps page id to table name, built by walking every table's chain.
    With many tables this has to be a lookup rather than a set of flags, and it
    is where Milestone 5 will add index names.
    """
    if page_id == META_PAGE_ID:
        return "meta"
    owner = owners.get(page_id)
    if owner is not None:
        return owner
    return "unallocated"


def _meta_summary(pager: Pager, raw: bytes) -> PageSummary:
    meta = pager.meta
    stored = int.from_bytes(raw[META_HEADER_SIZE - 4 : META_HEADER_SIZE], "little")
    computed = zlib.crc32(memoryview(raw)[: META_HEADER_SIZE - 4])
    return PageSummary(
        page_id=META_PAGE_ID,
        page_type=PageType.META.name,
        file_offset=0,
        lsn=meta.lsn,
        checksum=stored,
        checksum_valid=stored == computed,
        slot_count=0,
        live_record_count=0,
        free_space=pager.page_size - META_HEADER_SIZE,
        reclaimable_space=0,
        next_page_id=None,
        owner="meta",
    )


def summarize_page(
    pager: Pager,
    page_id: int,
    *,
    owners: Mapping[int, str] = MappingProxyType({}),
) -> PageSummary:
    """Describe one page without decoding its records.

    Never raises for a damaged page: a corrupt page is exactly what the
    inspector exists to show. Failures land in :attr:`PageSummary.error`.
    """
    raw = pager.read_raw(page_id)
    if page_id == META_PAGE_ID:
        return _meta_summary(pager, raw)

    # verify_checksum=False so a torn page still renders.
    page = Page(page_id, bytearray(raw), pager.page_size)
    stored = page.checksum
    valid = stored == page.compute_checksum()

    error: str | None = None
    try:
        page.validate()
        page_type = page.page_type.name
        slot_count = page.slot_count
        live = page.live_record_count
        free_space = page.free_space
        reclaimable = page.reclaimable_space
    except ChenDBError as exc:
        error = str(exc)
        page_type = f"UNKNOWN({raw[12]})"
        slot_count = page.slot_count
        live = 0
        free_space = 0
        reclaimable = 0

    next_page = page.next_page_id
    return PageSummary(
        page_id=page_id,
        page_type=page_type,
        file_offset=pager.file_offset(page_id),
        lsn=page.lsn,
        checksum=stored,
        checksum_valid=valid,
        slot_count=slot_count,
        live_record_count=live,
        free_space=free_space,
        reclaimable_space=reclaimable,
        next_page_id=None if next_page == INVALID_PAGE_ID else next_page,
        owner=_owner_for(page_id, owners),
        error=error,
    )


def _meta_header_fields(raw: bytes, meta: MetaPage) -> tuple[HeaderField, ...]:
    layout: tuple[tuple[str, int, int, Any, str], ...] = (
        ("magic", 0, 16, raw[:16].decode("ascii", "replace"), "file format marker"),
        ("format_version", 16, 4, meta.format_version, "on-disk layout version"),
        ("page_size", 20, 4, meta.page_size, "bytes per page, fixed at creation"),
        ("page_count", 24, 4, meta.page_count, "pages allocated in the file"),
        ("free_list_head", 28, 4, meta.free_list_head, "first recycled page"),
        (
            "catalog_tables_first",
            32,
            4,
            meta.catalog_tables_first,
            "first heap page of chendb_tables",
        ),
        ("catalog_tables_last", 36, 4, meta.catalog_tables_last, "its last page"),
        (
            "catalog_columns_first",
            40,
            4,
            meta.catalog_columns_first,
            "first heap page of chendb_columns",
        ),
        ("catalog_columns_last", 44, 4, meta.catalog_columns_last, "its last page"),
        ("next_table_id", 48, 4, meta.next_table_id, "id the next table will get"),
        ("lsn", 52, 8, meta.lsn, "reserved for the WAL (Milestone 9)"),
        ("flags", 60, 4, meta.flags, "reserved"),
        (
            "checksum",
            64,
            4,
            int.from_bytes(raw[64:68], "little"),
            "CRC32 over bytes 0..64",
        ),
    )
    return tuple(
        HeaderField(
            name=name,
            offset=offset,
            size=size,
            value=value,
            raw_hex=raw[offset : offset + size].hex(),
            description=description,
        )
        for name, offset, size, value, description in layout
    )


def _page_header_fields(raw: bytes, page: Page) -> tuple[HeaderField, ...]:
    next_page = page.next_page_id
    layout: tuple[tuple[str, int, int, Any, str], ...] = (
        ("checksum", 0, 4, page.checksum, "CRC32 over bytes 4..page_size"),
        ("lsn", 4, 8, page.lsn, "reserved for the WAL (Milestone 9)"),
        ("page_type", 12, 1, raw[12], "0 free, 1 meta, 2 heap, 3 schema"),
        ("flags", 13, 1, page.flags, "reserved"),
        ("slot_count", 14, 2, page.slot_count, "directory entries, dead included"),
        ("free_start", 16, 2, page.free_start, "end of slot directory (pd_lower)"),
        ("free_end", 18, 2, page.free_end, "start of record data (pd_upper)"),
        (
            "next_page_id",
            20,
            4,
            "none" if next_page == INVALID_PAGE_ID else next_page,
            "next page in this heap's chain",
        ),
    )
    return tuple(
        HeaderField(
            name=name,
            offset=offset,
            size=size,
            value=value,
            raw_hex=raw[offset : offset + size].hex(),
            description=description,
        )
        for name, offset, size, value, description in layout
    )


def inspect_page(
    pager: Pager,
    page_id: int,
    *,
    schema: Schema | None = None,
    owners: Mapping[int, str] = MappingProxyType({}),
) -> PageDetail:
    """Fully describe a page, decoding records when a ``schema`` is supplied.

    Without a schema the slots still render as raw hex — which is precisely the
    situation Milestone 4's catalog fixes, and worth being able to see.
    """
    raw = pager.read_raw(page_id)
    summary = summarize_page(pager, page_id, owners=owners)

    if page_id == META_PAGE_ID:
        return PageDetail(
            summary=summary,
            header_fields=_meta_header_fields(raw, pager.meta),
            slots=(),
            raw=raw,
            page_size=pager.page_size,
            header_size=META_HEADER_SIZE,
            slot_directory_end=META_HEADER_SIZE,
            free_start=META_HEADER_SIZE,
            free_end=pager.page_size,
        )

    page = Page(page_id, bytearray(raw), pager.page_size)
    slots: list[SlotDetail] = []
    for info in page.slot_directory():
        payload = raw[info.offset : info.offset + info.length] if info.is_live else b""
        record: RecordLayout | None = None
        decode_error: str | None = None
        if info.is_live and schema is not None:
            try:
                record = describe_record(schema, payload)
            except ChenDBError as exc:
                decode_error = str(exc)
        slots.append(
            SlotDetail(
                slot_id=info.slot_id,
                offset=info.offset,
                length=info.length,
                is_live=info.is_live,
                raw_hex=payload.hex(),
                record=record,
                decode_error=decode_error,
            )
        )

    return PageDetail(
        summary=summary,
        header_fields=_page_header_fields(raw, page),
        slots=tuple(slots),
        raw=raw,
        page_size=pager.page_size,
        header_size=PAGE_HEADER_SIZE,
        slot_directory_end=PAGE_HEADER_SIZE + page.slot_count * SLOT_SIZE,
        free_start=page.free_start,
        free_end=page.free_end,
    )


def render_page_map(detail: PageDetail, width: int = 60) -> str:
    """ASCII map of a page's regions, for the CLI and the docs."""
    total = detail.page_size

    def bar(label: str, start: int, end: int) -> str:
        span = max(1, round((end - start) / total * width)) if end > start else 0
        pct = (end - start) / total * 100
        return (
            f"│{'█' * span}{' ' * (width - span)}│ "
            f"{label:<16} [{start:>5}, {end:>5})  {pct:5.1f}%"
        )

    lines = [
        f"page {detail.summary.page_id}  type={detail.summary.page_type}  "
        f"size={total}B  offset={detail.summary.file_offset}",
        bar("header", 0, detail.header_size),
        bar("slot directory", detail.header_size, detail.slot_directory_end),
        bar("free space", detail.free_start, detail.free_end),
        bar("record data", detail.free_end, total),
    ]
    return "\n".join(lines)


def iter_page_summaries(
    pager: Pager,
    page_ids: Iterable[int],
    *,
    owners: Mapping[int, str] = MappingProxyType({}),
) -> list[PageSummary]:
    """Summarize several pages in one pass."""
    return [summarize_page(pager, page_id, owners=owners) for page_id in page_ids]
