"""Endpoint definitions, one module per resource family."""

from engine.server.routers import (
    buffer,
    catalog,
    databases,
    events,
    indexes,
    pages,
    query,
    sql,
    transactions,
)

__all__ = [
    "buffer",
    "catalog",
    "databases",
    "events",
    "indexes",
    "pages",
    "query",
    "sql",
    "transactions",
]
