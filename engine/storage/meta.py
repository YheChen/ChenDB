"""The meta page: page 0, the root of the whole database file.

Every persistent structure in the file is reachable from here.  The meta page
does *not* use the generic slotted-page header — it has a fixed layout of its
own so that the magic string lands at file offset 0 and ``head -c 16 x.chendb``
identifies the file.  SQLite does the same thing with its 100-byte header at
the start of page 1; PostgreSQL instead keeps cluster metadata in a separate
``pg_control`` file.

Layout (60 bytes, remainder of the page reserved and zero-filled)::

    off  size  field             notes
    ---  ----  ----------------  ------------------------------------------
      0    16  magic             b"ChenDB Format 1\\x00"
     16     4  format_version    bumped on any layout change
     20     4  page_size         bytes per page; fixed at creation
     24     4  page_count        pages allocated, including this one
     28     4  free_list_head    head of the recycled-page chain
     32     4  heap_first_page   M1: the single table's first heap page
     36     4  heap_last_page    M1: its last page — makes append O(1)
     40     4  schema_page_id    M1: page holding the JSON table descriptor
     44     8  lsn               reserved for Milestone 9 (WAL)
     52     4  flags             reserved
     56     4  checksum          CRC32 over bytes [0, 56)

``heap_first_page``, ``heap_last_page`` and ``schema_page_id`` are Milestone 1
scaffolding for the one-table-per-file limit.  Milestone 4 replaces all three
with a single ``catalog_root_page`` pointing at real system tables.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Final

from engine.errors import ChecksumMismatchError, CorruptDatabaseError
from engine.storage.constants import (
    FORMAT_VERSION,
    INVALID_PAGE_ID,
    MAGIC,
    PageType,
)

__all__ = ["META_HEADER_FORMAT", "META_HEADER_SIZE", "MetaPage"]

META_HEADER_FORMAT: Final[str] = "<16s9IQ2I"
META_HEADER_SIZE: Final[int] = struct.calcsize(META_HEADER_FORMAT)  # 60

#: The checksum is the last field, so it covers everything before itself.
_CHECKSUM_OFFSET: Final = META_HEADER_SIZE - 4
_CHECKSUM: Final = struct.Struct("<I")


@dataclass(slots=True)
class MetaPage:
    """Mutable in-memory view of page 0.

    Unlike :class:`~engine.storage.page.Page`, this is a decoded dataclass
    rather than a byte buffer.  The meta page is written on every allocation,
    so keeping it decoded avoids re-parsing it constantly; it is small enough
    that re-encoding on write costs nothing.
    """

    page_size: int
    page_count: int = 1
    format_version: int = FORMAT_VERSION
    free_list_head: int = INVALID_PAGE_ID
    catalog_tables_first: int = INVALID_PAGE_ID
    catalog_tables_last: int = INVALID_PAGE_ID
    catalog_columns_first: int = INVALID_PAGE_ID
    catalog_columns_last: int = INVALID_PAGE_ID
    next_table_id: int = 0
    lsn: int = 0
    flags: int = 0

    def to_bytes(self) -> bytes:
        """Encode into a full page, checksum included."""
        buf = bytearray(self.page_size)
        struct.pack_into(
            META_HEADER_FORMAT,
            buf,
            0,
            MAGIC,
            self.format_version,
            self.page_size,
            self.page_count,
            self.free_list_head,
            self.catalog_tables_first,
            self.catalog_tables_last,
            self.catalog_columns_first,
            self.catalog_columns_last,
            self.next_table_id,
            self.lsn,
            self.flags,
            0,  # checksum placeholder, filled in below
        )
        checksum = zlib.crc32(memoryview(buf)[:_CHECKSUM_OFFSET])
        _CHECKSUM.pack_into(buf, _CHECKSUM_OFFSET, checksum)
        return bytes(buf)

    @classmethod
    def from_bytes(cls, raw: bytes, *, verify_checksum: bool = True) -> MetaPage:
        """Decode page 0, validating magic, version and checksum."""
        if len(raw) < META_HEADER_SIZE:
            raise CorruptDatabaseError(
                f"file is {len(raw)} bytes, too short to hold a {META_HEADER_SIZE}-byte header"
            )
        (
            magic,
            format_version,
            page_size,
            page_count,
            free_list_head,
            catalog_tables_first,
            catalog_tables_last,
            catalog_columns_first,
            catalog_columns_last,
            next_table_id,
            lsn,
            flags,
            stored_checksum,
        ) = struct.unpack_from(META_HEADER_FORMAT, raw, 0)

        if magic != MAGIC:
            raise CorruptDatabaseError(
                f"bad magic {magic!r}: not a ChenDB database file"
            )
        if format_version != FORMAT_VERSION:
            hint = (
                " Version 1 files predate the catalog (Milestone 4) and cannot be "
                "upgraded in place; recreate the database."
                if format_version == 1
                else ""
            )
            raise CorruptDatabaseError(
                f"format version {format_version} is not supported "
                f"(this build understands version {FORMAT_VERSION}).{hint}"
            )
        if verify_checksum:
            actual = zlib.crc32(memoryview(raw)[:_CHECKSUM_OFFSET])
            if actual != stored_checksum:
                raise ChecksumMismatchError(
                    f"meta page: checksum mismatch "
                    f"(stored 0x{stored_checksum:08x}, computed 0x{actual:08x})"
                )
        if page_count < 1:
            raise CorruptDatabaseError(f"page_count {page_count} must be at least 1")

        return cls(
            page_size=page_size,
            page_count=page_count,
            format_version=format_version,
            free_list_head=free_list_head,
            catalog_tables_first=catalog_tables_first,
            catalog_tables_last=catalog_tables_last,
            catalog_columns_first=catalog_columns_first,
            catalog_columns_last=catalog_columns_last,
            next_table_id=next_table_id,
            lsn=lsn,
            flags=flags,
        )

    @property
    def page_type(self) -> PageType:
        """Always :attr:`PageType.META`; present so callers can treat it uniformly."""
        return PageType.META

    def __repr__(self) -> str:
        return (
            f"<MetaPage v{self.format_version} page_size={self.page_size} "
            f"pages={self.page_count} catalog={self.catalog_tables_first}/"
            f"{self.catalog_columns_first}>"
        )
