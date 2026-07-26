"""Index endpoints: what indexes exist, and what their trees look like.

    GET    /databases/{db}/indexes                list, with height and size
    POST   /databases/{db}/indexes                create one
    GET    /databases/{db}/indexes/{name}         the whole tree, node by node
    GET    /databases/{db}/indexes/{name}/search  what a point lookup does

Every handler follows the rule these routers have followed since Milestone 1:
**read engine state under the lock, build the response outside it**.  Decoding a
tree of several hundred nodes is not free, and a browser refreshing the index
view must never be able to stall a query.  The lock is held only for the reads;
:mod:`engine.server.mappers` runs after it is released, on a frozen snapshot
that cannot change underneath it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from engine.catalog.catalog import IndexInfo
from engine.errors import ChenDBError
from engine.index.bplustree import TreeSnapshot
from engine.serialization.types import DataType
from engine.server import mappers
from engine.server.deps import DatabaseDep, http_status_for
from engine.server.schemas.indexes import (
    CreateIndexRequest,
    IndexDetail,
    IndexListResponse,
    IndexSearchResponse,
)

router = APIRouter(prefix="/databases/{database_id}", tags=["indexes"])

#: Nodes returned by the tree view.  A tree over a large table has thousands of
#: leaves and no browser wants them all; the response says when it was cut short
#: rather than silently pretending the tree is smaller than it is.
DEFAULT_MAX_NODES = 512
MAX_MAX_NODES = 4096


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=http_status_for(exc),
        detail={"error": type(exc).__name__, "message": str(exc)},
    )


def _read_summary(db: Any, info: IndexInfo) -> tuple[IndexInfo, int, int, int]:
    """Height, entry count and page count. Caller must hold the engine lock."""
    tree = db.tree_for(info.name)
    return info, tree.height, tree.count(), len(tree.page_ids())


@router.get(
    "/indexes",
    response_model=IndexListResponse,
    summary="Every index, with its height and size",
)
def list_indexes(
    managed: DatabaseDep,
    table: Annotated[str | None, Query(description="Narrow to one table")] = None,
) -> IndexListResponse:
    try:
        with managed.use() as db:
            measured = [_read_summary(db, info) for info in db.indexes(table)]
    except ChenDBError as exc:
        raise _fail(exc) from exc

    return IndexListResponse(
        indexes=[
            mappers.index_summary_to_api(
                info, height=height, entry_count=entries, page_count=pages
            )
            for info, height, entries, pages in measured
        ]
    )


@router.post(
    "/indexes",
    response_model=IndexDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create an index over one column",
)
def create_index(managed: DatabaseDep, request: CreateIndexRequest) -> IndexDetail:
    try:
        with managed.use() as db:
            info = db.create_index(
                request.name, request.table, request.column, unique=request.unique
            )
            tree = db.tree_for(info.name)
            snapshot = tree.snapshot(max_nodes=DEFAULT_MAX_NODES)
            entries = tree.count()
            stats = tree.stats
    except ChenDBError as exc:
        raise _fail(exc) from exc

    return mappers.index_detail_to_api(info, snapshot, stats, entry_count=entries)


@router.get(
    "/indexes/{index_name}",
    response_model=IndexDetail,
    summary="One index, as a flat node list plus a root id",
)
def get_index(
    managed: DatabaseDep,
    index_name: str,
    max_nodes: Annotated[int, Query(ge=1, le=MAX_MAX_NODES)] = DEFAULT_MAX_NODES,
) -> IndexDetail:
    try:
        with managed.use() as db:
            info = db.index(index_name)
            if info is None:
                raise _not_found(index_name, db)
            tree = db.tree_for(info.name)
            snapshot: TreeSnapshot = tree.snapshot(max_nodes=max_nodes)
            entries = tree.count()
            stats = tree.stats
    except ChenDBError as exc:
        raise _fail(exc) from exc

    return mappers.index_detail_to_api(info, snapshot, stats, entry_count=entries)


@router.get(
    "/indexes/{index_name}/search",
    response_model=IndexSearchResponse,
    summary="Trace a point lookup: the path taken and what it found",
)
def search_index(
    managed: DatabaseDep,
    index_name: str,
    value: Annotated[str, Query(description="The key to look up, as text")],
) -> IndexSearchResponse:
    """Run one lookup and report the path, so the UI can highlight the descent.

    ``value`` arrives as text and is coerced to the index's declared type here.
    A query string has no types, and guessing from the JSON shape would make
    ``?value=1`` ambiguous between the integer and the string.
    """
    try:
        with managed.use() as db:
            info = db.index(index_name)
            if info is None:
                raise _not_found(index_name, db)
            key = info.encode(_coerce(value, info))
            tree = db.tree_for(info.name)
            before = tree.stats.nodes_visited
            matches = tree.search(key)
            visited = tree.stats.nodes_visited - before
            path = tree.descent_path(key)
            height = tree.height
    except ChenDBError as exc:
        raise _fail(exc) from exc

    return IndexSearchResponse(
        index_name=info.name,
        value=value,
        found=bool(matches),
        matches=[str(record_id) for record_id in matches],
        path=path,
        pages_visited=visited,
        height=height,
    )


def _coerce(value: str, info: IndexInfo) -> Any:
    """Turn a query-string value into something the index can encode."""
    if value == "":
        return None if info.data_type is not DataType.TEXT else ""
    try:
        match info.data_type:
            case DataType.INTEGER:
                return int(value)
            case DataType.FLOAT:
                return float(value)
            case DataType.BOOLEAN:
                return value.strip().lower() in ("1", "true", "t", "yes")
            case _:
                return value
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "InvalidKey",
                "message": (
                    f"{value!r} is not a valid {info.data_type.sql_name} value for "
                    f"index {info.name!r}"
                ),
            },
        ) from None


def _not_found(index_name: str, db: Any) -> HTTPException:
    known = ", ".join(info.name for info in db.indexes()) or "none"
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "IndexNotFound",
            "message": f"no index named {index_name!r}; this database has {known}",
        },
    )
