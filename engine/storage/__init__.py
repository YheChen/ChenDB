"""Storage engine: the file, its pages, and the records inside them.

Layering, bottom up::

    constants.py   page size, page types, sentinels — the file format's vocabulary
    page.py        a slotted page: variable-length records in a fixed-size block
    meta.py        page 0: the root of every persistent structure
    pager.py       page ids -> file offsets; allocation, checksums, fsync
    heap.py        a chain of pages holding one table's rows
    inspect.py     read-only views of all of the above, for tools and the UI

Each level knows only the one below it.  Milestone 7 slides a buffer pool in
between ``heap`` and ``pager`` without either of them changing.
"""

from engine.storage.constants import (
    DEFAULT_PAGE_SIZE,
    INVALID_PAGE_ID,
    MAX_PAGE_SIZE,
    META_PAGE_ID,
    MIN_PAGE_SIZE,
    PageType,
)
from engine.storage.heap import HeapFile, HeapStats, RecordId
from engine.storage.inspect import (
    PageDetail,
    PageSummary,
    SlotDetail,
    hexdump,
    inspect_page,
    render_page_map,
    summarize_page,
)
from engine.storage.meta import MetaPage
from engine.storage.page import PAGE_HEADER_SIZE, SLOT_SIZE, Page, SlotInfo
from engine.storage.pager import Pager, PagerStats

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "INVALID_PAGE_ID",
    "MAX_PAGE_SIZE",
    "META_PAGE_ID",
    "MIN_PAGE_SIZE",
    "PAGE_HEADER_SIZE",
    "SLOT_SIZE",
    "HeapFile",
    "HeapStats",
    "MetaPage",
    "Page",
    "PageDetail",
    "PageSummary",
    "PageType",
    "Pager",
    "PagerStats",
    "RecordId",
    "SlotDetail",
    "SlotInfo",
    "hexdump",
    "inspect_page",
    "render_page_map",
    "summarize_page",
]
