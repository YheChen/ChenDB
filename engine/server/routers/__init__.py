"""Endpoint definitions, one module per resource family."""

from engine.server.routers import (
    buffer,
    catalog,
    concurrency,
    databases,
    events,
    indexes,
    pages,
    query,
    sql,
    transactions,
    wal,
)

__all__ = [
    "buffer",
    "catalog",
    "concurrency",
    "databases",
    "events",
    "indexes",
    "pages",
    "query",
    "sql",
    "transactions",
    "wal",
]
