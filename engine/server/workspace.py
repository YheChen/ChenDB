"""Database lifecycle and the engine/HTTP concurrency boundary.

Two problems are solved here.

**Filesystem containment.** The browser never sees or supplies a path.  It
supplies a *database id* matched against :data:`DATABASE_ID_PATTERN`, which is
joined to the configured workspace root; the resolved path is then re-checked
to be inside that root, so a symlink or a decoded traversal cannot escape.

**Thread safety.** ``Database`` is not thread-safe: it holds one file handle
with a shared seek position.  FastAPI runs synchronous endpoints in a worker
threadpool, so several requests can arrive concurrently.  Each open database is
therefore wrapped in a :class:`ManagedDatabase` guarding it with a lock.

Diagnostics endpoints follow one rule: **take the lock, copy an immutable
snapshot, release the lock, then serialize.**  Holding an engine lock across
JSON encoding (let alone across a socket write) would let a slow client stall
every query. Copying frozen dataclasses instead means a response can never show
one buffer frame holding two pages, or a page list from two different instants.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from engine.database import Database
from engine.diagnostics import (
    DiagnosticSink,
    FanoutSink,
    RingBufferSink,
    Tracer,
)
from engine.errors import ChenDBError
from engine.server.config import ServerConfig
from engine.storage.constants import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MIN_PAGE_SIZE,
)

__all__ = [
    "DATABASE_ID_PATTERN",
    "DatabaseAlreadyExists",
    "DatabaseNotFound",
    "InvalidDatabaseId",
    "ManagedDatabase",
    "Workspace",
    "WorkspaceError",
]

#: Conservative on purpose: alphanumerics plus ``.``, ``-`` and ``_``, never
#: starting with a separator. Rejects ``..``, absolute paths, NUL bytes,
#: Windows device names and anything URL-encoded into a traversal.
DATABASE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DATABASE_SUFFIX: Final = ".chendb"

#: Reserved on Windows; harmless to reject everywhere.
_RESERVED_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10))}
)


class WorkspaceError(ChenDBError):
    """Base class for workspace-level failures."""


class InvalidDatabaseId(WorkspaceError):
    """A database id failed validation."""


class DatabaseNotFound(WorkspaceError):
    """No database with that id exists in the workspace."""


class DatabaseAlreadyExists(WorkspaceError):
    """A database with that id is already present."""


@dataclass(frozen=True, slots=True)
class DatabaseEntry:
    """A database file on disk, described without opening it."""

    database_id: str
    size_bytes: int
    modified_ns: int
    is_open: bool


class ManagedDatabase:
    """An open :class:`Database` plus the machinery the server wraps it in."""

    __slots__ = ("_db", "_fanout", "_lock", "_ring", "_tracer", "database_id")

    def __init__(
        self,
        database_id: str,
        db: Database,
        ring: RingBufferSink,
        fanout: FanoutSink,
        tracer: Tracer,
    ) -> None:
        self.database_id = database_id
        self._db = db
        self._ring = ring
        self._fanout = fanout
        self._tracer = tracer
        self._lock = threading.RLock()

    @contextmanager
    def use(self) -> Iterator[Database]:
        """Exclusive access to the engine.

        Keep the body short. Anything that can be done on a copied snapshot
        should be done after the lock is released.
        """
        with self._lock:
            yield self._db

    @property
    def events(self) -> RingBufferSink:
        """The retained-event buffer. Its own snapshots are already atomic."""
        return self._ring

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    def subscribe(self, sink: DiagnosticSink) -> None:
        """Attach a live listener, such as a WebSocket connection."""
        self._fanout.add(sink)

    def unsubscribe(self, sink: DiagnosticSink) -> None:
        self._fanout.remove(sink)

    @property
    def subscriber_count(self) -> int:
        # One slot is always the ring buffer; the rest are live listeners.
        return max(0, len(self._fanout.sinks) - 1)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def abandon(self) -> None:
        """Drop the handle the way a crash would. See :meth:`Database.abandon`."""
        with self._lock:
            self._db.abandon()


class Workspace:
    """Owns every open database and the directory they live in."""

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._root = config.workspace_path
        self._root.mkdir(parents=True, exist_ok=True)
        self._open: dict[str, ManagedDatabase] = {}
        self._lock = threading.Lock()

    # -- identity and paths ------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def validate_id(database_id: str) -> str:
        """Reject anything that is not a plain, contained name."""
        if not DATABASE_ID_PATTERN.match(database_id):
            raise InvalidDatabaseId(
                f"{database_id!r} is not a valid database id: use 1-64 characters "
                f"from A-Z a-z 0-9 . _ - and do not start with a separator"
            )
        if database_id.split(".")[0].casefold() in _RESERVED_NAMES:
            raise InvalidDatabaseId(f"{database_id!r} is a reserved name")
        return database_id

    def path_for(self, database_id: str) -> Path:
        """Resolve a database id to a contained path, or refuse.

        The containment check is repeated *after* resolution so a pre-existing
        symlink inside the workspace cannot redirect writes outside it.
        """
        self.validate_id(database_id)
        candidate = (self._root / f"{database_id}{DATABASE_SUFFIX}").resolve()
        if not candidate.is_relative_to(self._root):
            raise InvalidDatabaseId(
                f"{database_id!r} resolves outside the workspace directory"
            )
        return candidate

    # -- listing -----------------------------------------------------------

    def list_databases(self) -> list[DatabaseEntry]:
        """Every ``*.chendb`` file in the workspace, newest first."""
        entries: list[DatabaseEntry] = []
        for path in sorted(self._root.glob(f"*{DATABASE_SUFFIX}")):
            database_id = path.name.removesuffix(DATABASE_SUFFIX)
            if not DATABASE_ID_PATTERN.match(database_id):
                continue  # not something this server created; ignore it
            stat = path.stat()
            entries.append(
                DatabaseEntry(
                    database_id=database_id,
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    is_open=database_id in self._open,
                )
            )
        return sorted(entries, key=lambda entry: entry.modified_ns, reverse=True)

    def exists(self, database_id: str) -> bool:
        return self.path_for(database_id).exists()

    # -- lifecycle ---------------------------------------------------------

    def create(
        self, database_id: str, page_size: int = DEFAULT_PAGE_SIZE
    ) -> ManagedDatabase:
        """Create a new database file and open it."""
        path = self.path_for(database_id)
        if path.exists():
            raise DatabaseAlreadyExists(f"database {database_id!r} already exists")
        if not MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE:
            raise WorkspaceError(
                f"page_size must be between {MIN_PAGE_SIZE} and {MAX_PAGE_SIZE}"
            )
        return self._open_managed(database_id, page_size=page_size, create=True)

    def get(self, database_id: str) -> ManagedDatabase:
        """Return the open handle, opening the file if necessary."""
        with self._lock:
            managed = self._open.get(database_id)
            if managed is not None:
                return managed
        if not self.exists(database_id):
            raise DatabaseNotFound(f"no database {database_id!r} in this workspace")
        return self._open_managed(database_id, create=False)

    def _open_managed(
        self,
        database_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        create: bool,
    ) -> ManagedDatabase:
        path = self.path_for(database_id)
        with self._lock:
            existing = self._open.get(database_id)
            if existing is not None:
                return existing
            self._evict_if_needed()

            ring = RingBufferSink(capacity=self._config.trace_capacity)
            fanout = FanoutSink(ring)
            tracer = Tracer(fanout, self._config.default_trace_level)
            db = Database.open(
                path,
                create=create,
                page_size=page_size,
                tracer=tracer,
                database_id=database_id,
            )
            managed = ManagedDatabase(database_id, db, ring, fanout, tracer)
            self._open[database_id] = managed
            return managed

    def _evict_if_needed(self) -> None:
        """Close the least recently opened handle when at capacity.

        Caller must hold ``self._lock``. Closing is safe at any time: every
        write already went to the OS, and ``close`` fsyncs.
        """
        while len(self._open) >= self._config.max_open_databases:
            oldest_id = next(iter(self._open))
            self._open.pop(oldest_id).close()

    def close(self, database_id: str) -> None:
        with self._lock:
            managed = self._open.pop(database_id, None)
        if managed is not None:
            managed.close()

    def delete(self, database_id: str) -> None:
        """Close and remove a database file."""
        path = self.path_for(database_id)
        if not path.exists():
            raise DatabaseNotFound(f"no database {database_id!r} in this workspace")
        self.close(database_id)
        path.unlink()

    def crash(self, database_id: str) -> None:
        """Abandon a database without flushing, then forget the handle.

        The next request reopens the file, which runs recovery. Nothing here
        deletes anything: committed work survives because its commit record is
        already on the disk, and everything else is what recovery decides.

        This is destructive by design and exists for one reason. The explorer's
        recovery view should show recovery *happening*, and there is no honest
        way to do that with a clean shutdown.
        """
        with self._lock:
            managed = self._open.pop(database_id, None)
        if managed is None:
            if not self.exists(database_id):
                raise DatabaseNotFound(f"no database {database_id!r} in this workspace")
            return
        managed.abandon()

    def close_all(self) -> None:
        with self._lock:
            managed_list = list(self._open.values())
            self._open.clear()
        for managed in managed_list:
            managed.close()

    @property
    def open_count(self) -> int:
        return len(self._open)
