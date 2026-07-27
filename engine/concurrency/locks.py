"""Locks, waiting, and what to do when the waiting is circular.

    txn 3  holds X on users:(4,1)
    txn 5  wants X on users:(4,1)   →  waits for 3
    txn 5  holds X on users:(4,2)
    txn 3  wants X on users:(4,2)   →  waits for 5

        3 ──waits for──▶ 5
        ▲                │
        └──waits for─────┘        a cycle. Somebody has to lose.

**MVCC means readers are not here.** A reader takes a snapshot and reads an
older version; it never asks this module for anything. Everything below is
about writers conflicting with writers, which is the only conflict snapshot
isolation cannot make disappear.

Granularity
-----------
Locks are on **record ids** — a page and a slot — not on pages and not on
tables. That matters because ChenDB's undo log works in *pages*, so page-level
locking would have made two transactions inserting into the same heap page
conflict, which is most inserts. Row-level locking is the whole reason a second
writer is useful here.

The consequence is that the lock table can get large: one entry per row a
transaction touches. Real systems handle that with **lock escalation** — after
enough row locks on one table, take a table lock instead — which trades
concurrency for memory. ChenDB does not escalate, and
:data:`MAX_LOCKS_PER_TRANSACTION` is the honest failure instead.

Deadlock
--------
Detected, not prevented. The alternatives are worse for a teaching engine:

* **Prevention by ordering** — always take locks in a fixed order — requires
  knowing every lock you will need before you take the first, which a SQL
  statement does not.
* **Prevention by timeout** — give up after a while — cannot tell a deadlock
  from a slow transaction, so it either kills healthy work or leaves deadlocks
  sitting for the length of the timeout.

So this builds a wait-for graph and looks for a cycle, which is what
PostgreSQL, InnoDB and SQL Server all do. PostgreSQL waits one second before
even looking (``deadlock_timeout``), on the reasoning that most waits resolve
themselves and a graph search is wasted work; ChenDB checks immediately,
because a demonstration nobody waits a second for is a demonstration nobody
watches.

The victim is the **youngest** transaction in the cycle — the one that has done
the least work and will lose the least by being rolled back. InnoDB picks by
the number of rows changed, which is a better proxy and needs bookkeeping this
does not have.

**The victim is the transaction that fails, not the one that noticed.** Whoever
adds the closing edge is the one that runs the search, and it would be simpler
to have that transaction raise — but then the loser is decided by scheduling
rather than by cost, and the "youngest" rule would be decoration. So the
detector marks the victim, wakes everybody, and each waiter checks on the way
out whether it has been chosen. If the detector *is* the victim it raises
immediately, which is the common case with two transactions.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from engine.diagnostics.events import DeadlockEvent, LockEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.errors import DeadlockError, LockTimeout

__all__ = [
    "DEFAULT_LOCK_TIMEOUT_S",
    "MAX_LOCKS_PER_TRANSACTION",
    "LockManager",
    "LockMode",
    "LockRequest",
    "LockTableEntry",
    "ResourceId",
]

#: What a lock is taken on: a table name and a record id, or a table name alone
#: for a schema-level lock. A string rather than a structure so it can key a
#: dictionary and print itself into an event without a mapper.
ResourceId = str

#: How long a waiter blocks before giving up. Only reached when a lock is held
#: by a transaction that is neither committing nor deadlocked — an idle session
#: with an open transaction, which is a human problem rather than an engine one.
DEFAULT_LOCK_TIMEOUT_S: Final = 5.0

#: Locks one transaction may hold. Without escalation the table grows with rows
#: touched, and a runaway transaction would otherwise exhaust memory quietly.
MAX_LOCKS_PER_TRANSACTION: Final = 100_000


class LockMode(StrEnum):
    """Only two, because MVCC removed the need for the rest.

    A reader does not lock at all, so there is no need for the intention modes
    (``IS``, ``IX``, ``SIX``) that a hierarchical locker uses to say "I hold
    something finer-grained below this". ChenDB locks one level.
    """

    SHARED = "shared"
    """``SELECT … FOR UPDATE`` would take this. Nothing does yet — it exists
    because the compatibility matrix is not a matrix without it, and because
    leaving it out would make the lock table look like a mutex table."""

    EXCLUSIVE = "exclusive"
    """A writer. Conflicts with everything."""


#: Who may hold what alongside whom. The one interesting cell is shared/shared.
_COMPATIBLE: Final[dict[tuple[LockMode, LockMode], bool]] = {
    (LockMode.SHARED, LockMode.SHARED): True,
    (LockMode.SHARED, LockMode.EXCLUSIVE): False,
    (LockMode.EXCLUSIVE, LockMode.SHARED): False,
    (LockMode.EXCLUSIVE, LockMode.EXCLUSIVE): False,
}


def compatible(held: LockMode, wanted: LockMode) -> bool:
    return _COMPATIBLE[(held, wanted)]


@dataclass(frozen=True, slots=True)
class LockRequest:
    """One transaction's claim on one resource."""

    transaction_id: int
    resource: ResourceId
    mode: LockMode
    granted_at_ns: int = 0
    waiting_since_ns: int = 0

    @property
    def waiting(self) -> bool:
        return self.granted_at_ns == 0


@dataclass(slots=True)
class LockTableEntry:
    """Everything holding or wanting one resource."""

    resource: ResourceId
    holders: dict[int, LockMode] = field(default_factory=dict)
    waiters: list[LockRequest] = field(default_factory=list)

    def conflicts_with(self, transaction_id: int, mode: LockMode) -> list[int]:
        """Which transactions stand in the way. Empty means grant it."""
        return [
            holder
            for holder, held in self.holders.items()
            if holder != transaction_id and not compatible(held, mode)
        ]


class LockManager:
    """The lock table, the wait-for graph, and the deadlock detector.

    Thread-safe: every public method takes ``_lock``, and waiters block on a
    condition variable rather than spinning. That is not decoration — the whole
    point of this milestone is that two sessions run at once, and a lock manager
    that needed its own external serialisation would just move the bottleneck.
    """

    __slots__ = (
        "_condition",
        "_held_by",
        "_lock",
        "_stats",
        "_table",
        "_tracer",
        "_victims",
    )

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        self._tracer = tracer if tracer is not None else NULL_TRACER
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._table: dict[ResourceId, LockTableEntry] = {}
        #: Reverse index, so releasing a transaction is O(its locks) rather than
        #: O(the table). A transaction that touched three rows should not pay
        #: for a table holding a million.
        self._held_by: dict[int, dict[ResourceId, LockMode]] = {}
        #: Transactions chosen to break a cycle. A waiter checks this when it
        #: wakes; the entry is cleared when it raises or when it releases.
        self._victims: set[int] = set()
        self._stats = LockStats()

    # -- acquiring ---------------------------------------------------------

    def acquire(
        self,
        transaction_id: int,
        resource: ResourceId,
        mode: LockMode = LockMode.EXCLUSIVE,
        *,
        timeout: float = DEFAULT_LOCK_TIMEOUT_S,
    ) -> None:
        """Take a lock, waiting if necessary.

        Raises :class:`DeadlockError` if waiting would close a cycle, and
        :class:`LockTimeout` if it simply takes too long. Both leave the caller
        holding nothing new; the transaction is expected to roll back.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            entry = self._table.setdefault(resource, LockTableEntry(resource))
            if self._already_holds(transaction_id, resource, mode):
                return

            request = LockRequest(
                transaction_id=transaction_id,
                resource=resource,
                mode=mode,
                waiting_since_ns=time.monotonic_ns(),
            )
            self._check_victim(transaction_id)
            blockers = entry.conflicts_with(transaction_id, mode)
            if not blockers:
                self._grant(entry, request)
                return

            # Check before waiting, not after. A cycle that exists now will not
            # resolve itself, so sleeping on it only delays the answer.
            self._stats.waits += 1
            entry.waiters.append(request)
            self._emit(request, "waiting")
            try:
                self._detect(transaction_id, resource)
                while entry.conflicts_with(transaction_id, mode):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._stats.timeouts += 1
                        raise LockTimeout(
                            f"transaction {transaction_id} waited {timeout:g}s for "
                            f"{mode.value} on {resource!r}, still held by "
                            f"{sorted(entry.conflicts_with(transaction_id, mode))}"
                        )
                    self._condition.wait(timeout=remaining)
                    # The graph changed while we slept, in one of two ways: a
                    # cycle now exists, or somebody else found one and named us.
                    self._check_victim(transaction_id)
                    self._detect(transaction_id, resource)
            finally:
                if request in entry.waiters:
                    entry.waiters.remove(request)

            self._grant(entry, request)

    def _detect(self, transaction_id: int, resource: ResourceId) -> None:
        """Look for a cycle through ``transaction_id`` and act on it.

        Raises here only if *this* transaction is the chosen victim. Otherwise
        the victim is marked and woken, and this one keeps waiting — because
        once the victim rolls back, the lock it was holding is released and this
        wait resolves on its own.
        """
        cycle = self._find_cycle(transaction_id)
        if not cycle:
            return
        victim = self._choose_victim(cycle)
        if victim in self._victims:
            return  # already dealt with; do not count it twice
        self._emit_deadlock(cycle, victim)
        self._victims.add(victim)
        self._condition.notify_all()
        if victim == transaction_id:
            self._check_victim(transaction_id, cycle=cycle, resource=resource)

    def _check_victim(
        self,
        transaction_id: int,
        *,
        cycle: list[int] | None = None,
        resource: ResourceId | None = None,
    ) -> None:
        """Raise if this transaction has been chosen to break a cycle."""
        if transaction_id not in self._victims:
            return
        self._victims.discard(transaction_id)
        path = " → ".join(str(x) for x in cycle) + f" → {cycle[0]}" if cycle else ""
        where = f" waiting for {resource!r}" if resource else ""
        raise DeadlockError(
            f"deadlock: transaction {transaction_id}{where} is the youngest in "
            f"the cycle{' ' + path if path else ''}, so it is the one rolled "
            f"back. Retry it — the other transactions will now proceed."
        )

    def _already_holds(
        self, transaction_id: int, resource: ResourceId, mode: LockMode
    ) -> bool:
        """True when this transaction's existing lock already covers ``mode``.

        Re-taking a lock you hold has to be free — a transaction updating the
        same row twice must not wait for itself — and an exclusive lock covers
        a later shared request.
        """
        held = self._held_by.get(transaction_id, {}).get(resource)
        if held is None:
            return False
        return held is LockMode.EXCLUSIVE or held is mode

    def _grant(self, entry: LockTableEntry, request: LockRequest) -> None:
        held = self._held_by.setdefault(request.transaction_id, {})
        if len(held) >= MAX_LOCKS_PER_TRANSACTION:
            raise LockTimeout(
                f"transaction {request.transaction_id} holds "
                f"{MAX_LOCKS_PER_TRANSACTION:,} locks. ChenDB does not escalate "
                f"row locks to table locks, so a transaction this wide has no "
                f"cheaper representation available."
            )
        entry.holders[request.transaction_id] = request.mode
        held[request.resource] = request.mode
        self._stats.granted += 1
        self._emit(request, "granted")

    # -- releasing ---------------------------------------------------------

    def release_all(self, transaction_id: int) -> int:
        """Drop every lock a transaction holds. Returns how many.

        Locks are held until the transaction *ends*, never released early —
        that is strict two-phase locking, and it is what stops a second
        transaction reading a write that is about to be rolled back. Releasing
        at statement boundaries would be cheaper and would permit cascading
        aborts.
        """
        with self._condition:
            self._victims.discard(transaction_id)
            resources = self._held_by.pop(transaction_id, {})
            for resource in resources:
                entry = self._table.get(resource)
                if entry is None:
                    continue
                entry.holders.pop(transaction_id, None)
                if not entry.holders and not entry.waiters:
                    del self._table[resource]
            if resources:
                self._stats.released += len(resources)
                self._emit(
                    LockRequest(transaction_id, f"{len(resources)} lock(s)", LockMode.EXCLUSIVE),
                    "released",
                )
            # Wake everyone: which waiter can now proceed depends on modes, and
            # working that out here would duplicate the predicate each waiter
            # already re-evaluates on waking.
            self._condition.notify_all()
            return len(resources)

    # -- the wait-for graph ------------------------------------------------

    def wait_for_graph(self) -> dict[int, set[int]]:
        """Who is waiting for whom, right now.

        An edge from A to B means A wants something B holds. A cycle in this
        graph *is* a deadlock — that is the definition, not a heuristic.
        """
        with self._lock:
            return self._build_graph()

    def _build_graph(self) -> dict[int, set[int]]:
        graph: dict[int, set[int]] = {}
        for entry in self._table.values():
            for waiter in entry.waiters:
                blockers = entry.conflicts_with(waiter.transaction_id, waiter.mode)
                if blockers:
                    graph.setdefault(waiter.transaction_id, set()).update(blockers)
        return graph

    def _find_cycle(self, start: int) -> list[int]:
        """Depth-first search from ``start``, returning the cycle or ``[]``.

        Only from ``start``: a cycle can only have appeared because of the edge
        just added, so any cycle in the graph must pass through the transaction
        that added it. Searching the whole graph would find the same answer
        after more work.
        """
        graph = self._build_graph()
        path: list[int] = []
        on_path: set[int] = set()

        def walk(node: int) -> list[int]:
            if node in on_path:
                return path[path.index(node) :]
            path.append(node)
            on_path.add(node)
            for next_node in sorted(graph.get(node, ())):
                found = walk(next_node)
                if found:
                    return found
            path.pop()
            on_path.discard(node)
            return []

        return walk(start)

    @staticmethod
    def _choose_victim(cycle: list[int]) -> int:
        """The youngest transaction in the cycle.

        Ids increase, so the highest is the newest, and the newest has done the
        least work — the cheapest thing to throw away. InnoDB picks the one that
        changed the fewest rows, which is a better proxy for "cheapest" and
        needs per-transaction accounting this does not keep.
        """
        return max(cycle)

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> LockTableSnapshot:
        """A frozen copy, for the lock table view.

        Copied under the lock and read outside it, like every other diagnostics
        view in the engine: a browser polling the lock table must never be able
        to stall a transaction waiting on one.
        """
        with self._lock:
            entries = tuple(
                LockTableEntry(
                    resource=entry.resource,
                    holders=dict(entry.holders),
                    waiters=list(entry.waiters),
                )
                for entry in sorted(self._table.values(), key=lambda e: e.resource)
            )
            return LockTableSnapshot(
                entries=entries,
                wait_for=({k: set(v) for k, v in self._build_graph().items()}),
                # replace(), not vars(): LockStats is slots=True and has
                # no __dict__. The buffer pool hit this exact wall.
                stats=replace(self._stats),
            )

    def held_by(self, transaction_id: int) -> dict[ResourceId, LockMode]:
        with self._lock:
            return dict(self._held_by.get(transaction_id, {}))

    def __iter__(self) -> Iterator[LockTableEntry]:
        return iter(self.snapshot().entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._table)

    # -- diagnostics -------------------------------------------------------

    def _emit(self, request: LockRequest, action: str) -> None:
        if not self._tracer.summary:
            return
        self._tracer.emit(
            LockEvent(
                transaction_id=request.transaction_id,
                resource=request.resource,
                mode=request.mode.value,  # type: ignore[arg-type]
                action=action,  # type: ignore[arg-type]
            )
        )

    def _emit_deadlock(self, cycle: list[int], victim: int) -> None:
        self._stats.deadlocks += 1
        if not self._tracer.summary:
            return
        self._tracer.emit(
            DeadlockEvent(
                cycle=tuple(cycle),
                victim=victim,
                waiters=len(self._build_graph()),
            )
        )

    def __repr__(self) -> str:
        return f"<LockManager resources={len(self._table)} deadlocks={self._stats.deadlocks}>"


@dataclass(slots=True)
class LockStats:
    granted: int = 0
    released: int = 0
    waits: int = 0
    """Requests that had to block. The ratio against ``granted`` is how much
    contention there actually is, as opposed to how much the design allows."""
    timeouts: int = 0
    deadlocks: int = 0


@dataclass(frozen=True, slots=True)
class LockTableSnapshot:
    entries: tuple[LockTableEntry, ...]
    wait_for: dict[int, set[int]]
    stats: LockStats
