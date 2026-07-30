"""On-disk constants shared by the storage layer.

Everything here is part of the *file format*.  Changing a value in this module
changes the layout of the database file and requires a format version bump.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "FORMAT_VERSION",
    "INVALID_PAGE_ID",
    "MAGIC",
    "MAX_PAGE_SIZE",
    "META_PAGE_ID",
    "MIN_PAGE_SIZE",
    "PageType",
]

# 4 KiB matches the default page size of SQLite and the block size of most
# filesystems, so one page read maps to one filesystem block.  PostgreSQL uses
# 8 KiB instead, trading a little more read amplification for a shorter page
# chain and a larger maximum tuple size.
DEFAULT_PAGE_SIZE = 4096

# The page header stores free-space pointers as uint16, so a page can never
# exceed 64 KiB.  The lower bound keeps room for a header plus a useful tuple;
# tests use small pages deliberately to force page splits cheaply.
MIN_PAGE_SIZE = 256
MAX_PAGE_SIZE = 65536

#: Sentinel stored in ``next_page_id`` to mean "no such page".  Page 0 is a
#: real page (the meta page), so 0 cannot serve as the null pointer.
INVALID_PAGE_ID = 0xFFFFFFFF

#: The meta page always lives at page 0, i.e. file offset 0.
META_PAGE_ID = 0

#: Bumped whenever the layout of any on-disk structure changes.
#:
#: 1, Milestone 1: one table per file, a JSON schema page.
#: 2, Milestone 4: system tables, many tables per file.
#: 3, Milestone 5: a third system table for indexes, and a single object-id
#:     counter shared by tables and indexes.
FORMAT_VERSION = 5

#: Written at file offset 0 so ``head -c 16`` identifies a ChenDB file.
MAGIC = b"ChenDB Format 1\x00"
assert len(MAGIC) == 16


class PageType(IntEnum):
    """Discriminator stored in byte 12 of every page header.

    Values are frozen once shipped: they are persisted on disk.
    """

    FREE = 0
    """Unallocated. Sits on the pager's free list; ``next_page_id`` chains it."""

    META = 1
    """Page 0 only. Uses its own header layout (see ``meta.py``)."""

    HEAP = 2
    """A slotted page holding table tuples."""

    SCHEMA = 3
    """Retired in Milestone 4.

    Version 1 stored a JSON table descriptor here. The catalog now lives in
    ordinary ``HEAP`` pages, the way PostgreSQL stores ``pg_class`` and
    ``pg_attribute``. The value stays reserved so it is never reused for
    something else.
    """

    BTREE_INTERNAL = 4
    """A B+ tree routing node: separators and child page ids, no values."""

    BTREE_LEAF = 5
    """A B+ tree leaf: keys and record ids, linked to the next leaf."""

    OVERFLOW = 6
    """Reserved: spill page for records larger than one page."""
