"""The meta page: page 0, the root of the whole database file.

Every persistent structure in the file is reachable from here.  The meta page
does *not* use the generic slotted-page header — it has a fixed layout of its
own so that the magic string lands at file offset 0 and ``head -c 16 x.chendb``
identifies the file.  SQLite does the same thing with its 100-byte header at
the start of page 1; PostgreSQL instead keeps cluster metadata in a separate
``pg_control`` file.

Layout, format version 3 (76 bytes; the rest of the page is zero-filled)::

    off  size  field                   notes
    ---  ----  ----------------------  ------------------------------------
      0    16  magic                   b"ChenDB Format 1\\x00"
     16     4  format_version          bumped on any layout change
     20     4  page_size               bytes per page; fixed at creation
     24     4  page_count              pages allocated, including this one
     28     4  free_list_head          head of the recycled-page chain
     32     4  catalog_tables_first    chendb_tables heap, first page
     36     4  catalog_tables_last     …and last, so append is O(1)
     40     4  catalog_columns_first   chendb_columns heap
     44     4  catalog_columns_last
     48     4  catalog_indexes_first   chendb_indexes heap        (M5)
     52     4  catalog_indexes_last
     56     4  next_object_id          shared id counter          (M5)
     60     8  lsn                     reserved for Milestone 9 (WAL)
     68     4  flags                   reserved
     72     4  checksum                CRC32 over bytes [0, 72)

Only these six pointers are needed to *start* reading; everything else — every
table's heap, every index's root — is a row in a system table.  That is the
whole point of the catalog: adding a table or an index is an insert, not a
file-format change.  Milestone 5 still had to touch the format, but only because
it added a new *system* table, which is exactly the kind of change that cannot
bootstrap itself.

``next_object_id`` replaces version 2's ``next_object_id``.  Tables and indexes
draw ids from one sequence, so an id identifies a catalog object without also
having to say what kind it is — which is what PostgreSQL's global OID counter
buys, and why ``pg_class`` and ``pg_index`` can refer to each other by bare id.
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

META_HEADER_FORMAT: Final[str] = "<16s11IQ2I"
META_HEADER_SIZE: Final[int] = struct.calcsize(META_HEADER_FORMAT)  # 76

#: The checksum is the last field, so it covers everything before itself.
_CHECKSUM_OFFSET: Final = META_HEADER_SIZE - 4
_CHECKSUM: Final = struct.Struct("<I")

#: What to tell someone holding a file this build cannot read.  There is no
#: in-place upgrade path: every version so far moved where the catalog lives, so
#: rewriting a file would mean carrying a reader for each old layout — worth it
#: for a shipped database, not for a teaching one.
_UPGRADE_HINTS: Final[dict[int, str]] = {
    1: (
        " Version 1 files predate the catalog (Milestone 4) and cannot be "
        "upgraded in place; recreate the database."
    ),
    2: (
        " Version 2 files predate the index catalog (Milestone 5) and cannot be "
        "upgraded in place; recreate the database."
    ),
}


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
    catalog_indexes_first: int = INVALID_PAGE_ID
    catalog_indexes_last: int = INVALID_PAGE_ID
    next_object_id: int = 0
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
            self.catalog_indexes_first,
            self.catalog_indexes_last,
            self.next_object_id,
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
            catalog_indexes_first,
            catalog_indexes_last,
            next_object_id,
            lsn,
            flags,
            stored_checksum,
        ) = struct.unpack_from(META_HEADER_FORMAT, raw, 0)

        if magic != MAGIC:
            raise CorruptDatabaseError(
                f"bad magic {magic!r}: not a ChenDB database file"
            )
        if format_version != FORMAT_VERSION:
            hint = _UPGRADE_HINTS.get(format_version, "")
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
            catalog_indexes_first=catalog_indexes_first,
            catalog_indexes_last=catalog_indexes_last,
            next_object_id=next_object_id,
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
