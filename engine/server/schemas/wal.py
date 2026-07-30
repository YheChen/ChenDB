"""Write-ahead log and recovery API models.

Two things these are shaped to make visible, because neither is visible from
anywhere else in the explorer:

**What a commit costs.** ``mean_sync_ns`` divided into a second is the ceiling
on commit throughput, and it has nothing to do with how much work each
transaction did. It is the number that explains why real systems invented group
commit.

**What recovery decided, and why.** ``skip`` is as interesting as ``redo``: it
means the page already carried an LSN at or past the record's, so the change was
present. A recovery view that only showed the work done would hide the work the
last checkpoint saved.

Page images are **not** sent. A log record carries up to two of them, so a
thousand records is eight megabytes of JSON that no panel renders. The sizes
are, because the sizes are the interesting part.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from engine.server.schemas.common import ApiModel

__all__ = [
    "CheckpointResponse",
    "CrashResponse",
    "RecoveryActionModel",
    "RecoveryReportModel",
    "WalRecordModel",
    "WalResponse",
    "WalStatsModel",
]


class WalRecordModel(ApiModel):
    """One entry in the log."""

    lsn: int = Field(description="This record's byte offset in the log stream")
    prev_lsn: int = Field(
        description="The same transaction's previous record, or 0 for its first. "
        "ARIES calls this the backward chain."
    )
    transaction_id: int = Field(description="0 for engine bookkeeping outside any")
    record_type: Literal["update", "commit", "abort", "checkpoint"]
    page_id: int
    size: int = Field(description="Bytes on disk, header and images together")
    before_image_size: int = Field(
        description="Non-zero only on a transaction's first write to a page; "
        "first-write-wins, the same rule the in-memory undo log follows"
    )
    after_image_size: int


class WalStatsModel(ApiModel):
    records_appended: int
    records_coalesced: int = Field(
        description="Appends that replaced a staged record for the same page "
        "instead of following it. Every one is a page image not written, and "
        "in a bulk insert it is almost all of them."
    )
    bytes_appended: int
    flushes: int = Field(description="Times the buffer reached the OS")
    syncs: int = Field(description="Times it reached the disk. The expensive one.")
    mean_sync_ns: float = Field(
        description="Average fsync. One second divided by this is the hard "
        "ceiling on commits per second."
    )
    checkpoints: int
    bytes_reclaimed: int


class WalResponse(ApiModel):
    enabled: bool = Field(description="False for a handle opened without a log")
    path: str = Field(description="Filename only, never a path from the host")
    base_lsn: int = Field(
        description="The LSN of the log file's first byte. Non-zero after a "
        "checkpoint has truncated it, which is why the meta page has to carry it."
    )
    next_lsn: int = Field(description="What the next appended record will get")
    flushed_lsn: int = Field(description="Everything below this has reached the OS")
    buffered_bytes: int = Field(description="Staged in memory, not yet written")
    size_bytes: int = Field(description="The log file on disk")
    records: list[WalRecordModel]
    truncated_tail: bool = Field(
        description="The last record in the file is incomplete. Normal after a "
        "crash. The process died part-way through a write."
    )
    total_records: int = Field(description="Before the response limit was applied")
    stats: WalStatsModel


class RecoveryActionModel(ApiModel):
    phase: Literal["analysis", "redo", "undo"]
    lsn: int
    page_id: int
    decision: Literal["redo", "skip", "undo"]
    reason: str


class RecoveryReportModel(ApiModel):
    ran: bool = Field(
        description="False after a clean shutdown, because a clean shutdown ends "
        "with a checkpoint and leaves an empty log. So this means exactly 'the "
        "previous process did not close properly'."
    )
    records_scanned: int
    truncated_tail: bool
    winners: list[int] = Field(description="Committed or aborted; their work stands")
    losers: list[int] = Field(description="Caught in flight; their work was undone")
    pages_redone: int
    pages_skipped: int = Field(
        description="Records the page had already got. High relative to redone "
        "means the last checkpoint did its job."
    )
    pages_undone: int
    highest_lsn: int
    duration_ns: int
    phase_ns: dict[str, int]
    summary: str


class CheckpointResponse(ApiModel):
    pages_flushed: int
    bytes_reclaimed: int
    log_size_before: int
    log_size_after: int = Field(description="Zero: a checkpoint discards the log")
    base_lsn: int
    message: str


class CrashResponse(ApiModel):
    """What the crash button did."""

    message: str
    recovered: RecoveryReportModel = Field(
        description="Recovery already ran: reopening the file is what triggers "
        "it, and the response would be a lie if it reported the pre-crash state."
    )
    rows_before: dict[str, int] = Field(
        description="Row count per table before the crash, so the caller can "
        "show what survived without having to have asked first"
    )
    rows_after: dict[str, int]
