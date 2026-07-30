"""Page inspector endpoints.

Everything returned here is read from the real file through
:mod:`engine.storage.inspect`.  Nothing is reconstructed or approximated in the
frontend. The inspector shows the bytes that are actually on disk.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from engine.errors import ChenDBError, PageNotFoundError
from engine.server import mappers
from engine.server.deps import DatabaseDep, http_status_for
from engine.server.schemas.pages import PageDetailModel, PageListResponse

router = APIRouter(prefix="/databases/{database_id}", tags=["pages"])


@router.get("/pages", response_model=PageListResponse, summary="List every page")
def list_pages(managed: DatabaseDep) -> PageListResponse:
    """Summarize all pages in the file.

    Reads every page, so it is O(pages) syscalls, fine for the sizes a
    visualizer works with, and it will get cheap once Milestone 7's buffer pool
    keeps hot pages in memory.
    """
    with managed.use() as db:
        summaries = db.page_summaries()
        page_size = db.page_size
        page_count = db.page_count

    return PageListResponse(
        pages=[mappers.page_summary_to_api(summary) for summary in summaries],
        page_size=page_size,
        page_count=page_count,
        total_bytes=page_size * page_count,
    )


@router.get(
    "/pages/{page_id}",
    response_model=PageDetailModel,
    summary="Inspect one page: header, slot directory, raw bytes, decoded records",
)
def get_page(managed: DatabaseDep, page_id: int) -> PageDetailModel:
    if page_id < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "InvalidPageId", "message": "page_id must be >= 0"},
        )
    try:
        with managed.use() as db:
            detail = db.page_detail(page_id)
    except PageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "PageNotFound", "message": str(exc)},
        ) from exc
    except ChenDBError as exc:
        raise HTTPException(
            status_code=http_status_for(exc),
            detail={"error": type(exc).__name__, "message": str(exc)},
        ) from exc

    # Mapping happens after the lock is released: it is pure CPU work on an
    # immutable snapshot.
    return mappers.page_detail_to_api(detail)
