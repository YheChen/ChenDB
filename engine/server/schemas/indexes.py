"""Index API models.

The tree is sent as a **flat node list plus a root page id**, not as nested
JSON.  Same shape, and same reasons, as the AST in Milestone 2 and the plan in
Milestone 3:

* a client that wants one node does not have to walk a recursive structure;
* a cycle in a corrupt tree becomes a visibly duplicated entry rather than a
  response that never ends;
* the renderer computes its own layout anyway, so the nesting would be thrown
  away on arrival.

Keys arrive already rendered as strings.  Encoded keys are order-preserving byte
strings that only :mod:`engine.index.key` can interpret, so sending them raw
would force the browser to reimplement the codec — and the visualizer showing
something different from the engine is precisely the failure mode this project
is meant to avoid.
"""

from __future__ import annotations

from pydantic import Field

from engine.server.schemas.common import ApiModel, RequestModel

__all__ = [
    "CreateIndexRequest",
    "IndexDetail",
    "IndexListResponse",
    "IndexSearchResponse",
    "IndexStatsModel",
    "IndexSummary",
    "TreeNodeModel",
    "TreeSnapshotModel",
]


class IndexSummary(ApiModel):
    index_id: int
    name: str
    table_name: str
    column_name: str
    column_position: int = Field(
        description="Which column of the record the key comes from"
    )
    data_type: str
    unique: bool
    root_page: int = Field(
        description="Page the tree is rooted at. Changes when the root splits."
    )
    height: int = Field(description="Levels from root to leaf; 1 for a single leaf")
    entry_count: int
    page_count: int


class TreeNodeModel(ApiModel):
    """One B+ tree node, decoded for display."""

    page_id: int
    level: int = Field(description="0 at the leaves, increasing toward the root")
    is_leaf: bool
    keys: list[str] = Field(
        description="Rendered keys or separators, in slot order. '-∞' is the "
        "sentinel every internal node starts with."
    )
    children: list[int] = Field(description="Child page ids; empty for a leaf")
    record_ids: list[str] = Field(
        description="'(page,slot)' per entry; empty for an internal node"
    )
    next_leaf_id: int | None = Field(
        description="The next leaf in key order, or null at the end of the chain"
    )
    free_bytes: int
    entry_count: int


class TreeSnapshotModel(ApiModel):
    root_page_id: int
    height: int
    nodes: list[TreeNodeModel]
    truncated: bool = Field(
        description="True when the node budget was hit and the tree is only partly sent"
    )


class IndexStatsModel(ApiModel):
    """What the index has done since the database was opened."""

    searches: int
    inserts: int
    deletes: int
    splits: int
    root_splits: int
    range_scans: int
    nodes_visited: int
    leaves_visited: int
    pages_allocated: int


class IndexDetail(ApiModel):
    index: IndexSummary
    tree: TreeSnapshotModel
    stats: IndexStatsModel


class IndexListResponse(ApiModel):
    indexes: list[IndexSummary]


class IndexSearchResponse(ApiModel):
    """One traced point lookup.

    ``path`` is what the tree view highlights: the page ids from root to leaf.
    ``pages_visited`` can exceed its length when duplicates spill across leaves
    and the search has to step right — which is exactly the case worth seeing.
    """

    index_name: str
    value: str
    found: bool
    matches: list[str] = Field(description="'(page,slot)' per matching row")
    path: list[int] = Field(description="Page ids from the root to the leaf reached")
    pages_visited: int
    height: int


class CreateIndexRequest(RequestModel):
    """Programmatic index creation.

    ``CREATE INDEX`` through ``POST /query`` is the primary path; this stays for
    clients that would rather not build SQL strings, matching the table endpoint.
    """

    name: str = Field(min_length=1, max_length=64)
    table: str = Field(min_length=1, max_length=64)
    column: str = Field(min_length=1, max_length=64)
    unique: bool = False
