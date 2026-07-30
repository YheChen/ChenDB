"""Lock table, wait-for graph and session API models.

The wait-for graph goes out as an **adjacency list**, not a rendered picture
and not a flat list of pairs. A cycle is the only thing anybody looks at one of
these for, and a client that has the adjacency can find one; a client given
pairs has to rebuild it first.

Sessions exist because HTTP has none. Two browser consoles talking to one
database need two transactions, and a transaction belongs to whoever opened it,
so the session id is in the path rather than in a cookie, which keeps it visible
in the request log and in the explorer's own URL.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from engine.server.schemas.common import ApiModel

__all__ = [
    "LockEntryModel",
    "LockStatsModel",
    "LockTableResponse",
    "SessionListResponse",
    "SessionModel",
    "WaitForEdge",
]


class LockEntryModel(ApiModel):
    """One resource, everyone holding it, and everyone queued behind them."""

    resource: str = Field(
        description="'table:page.slot', a single row. Not a page and not a "
        "table: page granularity would make two sessions inserting into the "
        "same heap page conflict, which is most inserts."
    )
    holders: dict[str, Literal["shared", "exclusive"]] = Field(
        description="Transaction id (as a string, because JSON objects have "
        "string keys) to the mode it holds"
    )
    waiters: list[int] = Field(description="Transactions blocked on this, in order")


class WaitForEdge(ApiModel):
    waiter: int
    blockers: list[int]


class LockStatsModel(ApiModel):
    granted: int
    released: int
    waits: int = Field(
        description="Requests that had to block. Against `granted` this is how "
        "much contention there actually is, rather than how much the design "
        "permits."
    )
    timeouts: int
    deadlocks: int


class LockTableResponse(ApiModel):
    entries: list[LockEntryModel]
    wait_for: list[WaitForEdge] = Field(
        description="The graph. A cycle in it is a deadlock (that is the "
        "definition, not a heuristic) and it is always empty by the time you "
        "read it, because the detector breaks cycles as they form."
    )
    stats: LockStatsModel
    readers_blocked: int = Field(
        default=0,
        description="Always zero, and reported so the zero is visible. Under "
        "MVCC a reader takes no lock at all; every entry above is a writer.",
    )


class SessionModel(ApiModel):
    """One console's worth of state."""

    session: str
    transaction_id: int | None = Field(description="Null when nothing is open")
    state: str | None = None
    isolation_level: str | None = None
    snapshot: str | None = Field(
        default=None, description="xmin, xmax and the active set, rendered"
    )
    snapshots_taken: int = Field(
        default=0,
        description="One under REPEATABLE READ however many statements ran; one "
        "per statement under READ COMMITTED. The difference between the levels, "
        "made countable.",
    )
    statements: int = 0
    rows_created: int = 0
    rows_deleted: int = 0
    locks_held: int = 0
    waiting_for: list[int] = Field(
        default_factory=list, description="Transactions this one is blocked on"
    )


class SessionListResponse(ApiModel):
    sessions: list[SessionModel]
    frozen_xid: int = Field(
        description="Transaction ids below this committed before this process "
        "started. ChenDB's entire commit log, in one number."
    )
    next_xid: int
    oldest_snapshot_xmin: int = Field(
        description="Vacuum's horizon. A long-running transaction holds this "
        "down and stops dead versions being reclaimed; the same mechanism "
        "behind PostgreSQL's most common 'why is my disk full'."
    )
