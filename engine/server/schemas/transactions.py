"""Transaction API models.

The interesting number here is ``pages_held``: the size of the undo log, which
is the price of being able to change your mind. It grows with *distinct pages
touched*, not with rows written, and the difference between those two is the
whole point of first-write-wins — a transaction that updates the same page ten
thousand times still holds one before-image.

``undo_bytes`` makes that concrete in a unit the browser can render as a bar
next to the buffer pool's, so the two memory budgets are comparable at a glance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from engine.server.schemas.common import ApiModel

__all__ = [
    "TransactionListResponse",
    "TransactionModel",
    "TransactionResultResponse",
    "UndoRecordModel",
]


class UndoRecordModel(ApiModel):
    """One before-image: what a page looked like before this transaction."""

    sequence: int = Field(
        description="Capture order. Rollback replays these in reverse, though "
        "with one image per page the order does not actually matter."
    )
    page_id: int
    before_image_size: int = Field(description="Always one page")
    reason: str = Field(
        description="What was about to write the page — 'insert', 'index split' "
        "and so on. Diagnostic only; the undo itself needs no reason."
    )


class TransactionModel(ApiModel):
    transaction_id: int
    state: Literal["active", "failed", "committed", "aborted"] = Field(
        description="'failed' is open but doomed: a statement in it raised, so "
        "only COMMIT (which rolls back) and ROLLBACK are accepted."
    )
    implicit: bool = Field(
        description="True when the engine opened this around a bare statement "
        "rather than the client sending BEGIN"
    )
    statements: int
    pages_written: int = Field(
        description="Page writes seen, including repeats of the same page"
    )
    pages_held: int = Field(
        description="Distinct pages with a before-image. Zero once finished — "
        "the undo log is released at commit and at rollback."
    )
    pages_restored: int = Field(
        description="Pages written back by a rollback. Zero for anything else."
    )
    undo_bytes: int = Field(description="pages_held * page_size, while active")
    duration_ns: int
    records: list[UndoRecordModel] = Field(
        default_factory=list,
        description="Populated for the active transaction only; finished ones "
        "have released their undo log.",
    )


class TransactionListResponse(ApiModel):
    active: TransactionModel | None = Field(description="Null when the database is idle")
    history: list[TransactionModel] = Field(
        description="Finished transactions, oldest first, capped at the "
        "manager's history limit"
    )
    history_limit: int
    in_transaction: bool
    is_failed: bool = Field(
        description="A statement in the open transaction raised. The UI should "
        "offer rollback and stop offering anything else."
    )
    in_explicit_transaction: bool = Field(
        description="An implicit transaction is invisible to the client: it "
        "opens and commits inside one statement. Only an explicit one is "
        "something the user can COMMIT or ROLLBACK."
    )
    undo_bytes: int = Field(description="Held right now, across the whole database")


class TransactionResultResponse(ApiModel):
    """What BEGIN, COMMIT or ROLLBACK did."""

    action: Literal["begin", "commit", "rollback"]
    transaction: TransactionModel
    message: str
