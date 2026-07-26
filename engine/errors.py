"""Exception hierarchy for the ChenDB engine.

Every error raised by engine code derives from :class:`ChenDBError`, so an
embedding application can catch the whole engine with one ``except`` clause
without also swallowing genuine Python bugs such as ``AttributeError``.

The tree mirrors the layering of the engine itself::

    ChenDBError
    ├── StorageError          storage/  — pages, files, the page allocator
    │   ├── CorruptDatabaseError
    │   ├── CorruptPageError
    │   │   └── ChecksumMismatchError
    │   ├── PageNotFoundError
    │   ├── RecordTooLargeError
    │   └── RecordNotFoundError
    ├── SerializationError    serialization/ — encoding tuples to bytes
    │   ├── SchemaMismatchError
    │   ├── TypeMismatchError
    │   └── NullConstraintViolation
    └── SchemaError           an invalid schema *definition*
"""

from __future__ import annotations

__all__ = [
    "ChecksumMismatchError",
    "ChenDBError",
    "CorruptDatabaseError",
    "CorruptPageError",
    "NullConstraintViolation",
    "PageNotFoundError",
    "RecordNotFoundError",
    "RecordTooLargeError",
    "SchemaError",
    "SchemaMismatchError",
    "SerializationError",
    "StorageError",
    "TypeMismatchError",
]


class ChenDBError(Exception):
    """Base class for every error raised by the engine."""


# --------------------------------------------------------------------------
# Storage layer
# --------------------------------------------------------------------------


class StorageError(ChenDBError):
    """Base class for failures in the storage engine."""


class CorruptDatabaseError(StorageError):
    """The database file as a whole is not usable.

    Raised for a bad magic number, an unsupported format version, or a file
    length that is not a whole number of pages.
    """


class CorruptPageError(StorageError):
    """A single page violates an on-disk invariant."""


class ChecksumMismatchError(CorruptPageError):
    """A page's stored CRC32 does not match its contents.

    In a real system this usually means a torn write (the OS wrote part of a
    page before losing power) or bit rot in the storage medium.
    """


class PageNotFoundError(StorageError):
    """A page id was requested that lies outside the allocated file."""


class RecordTooLargeError(StorageError):
    """A record cannot fit on an empty page.

    Real databases solve this with out-of-line storage: PostgreSQL's TOAST
    tables and SQLite's overflow page chains.  ChenDB does not (yet).
    """


class RecordNotFoundError(StorageError):
    """A :class:`~engine.storage.heap.RecordId` refers to a dead or absent slot."""


# --------------------------------------------------------------------------
# Serialization layer
# --------------------------------------------------------------------------


class SerializationError(ChenDBError):
    """Base class for tuple encode/decode failures."""


class SchemaMismatchError(SerializationError):
    """A row has the wrong number of values for its schema."""


class TypeMismatchError(SerializationError):
    """A value's Python type does not match its declared column type."""


class NullConstraintViolation(SerializationError):
    """``None`` was supplied for a ``NOT NULL`` column."""


# --------------------------------------------------------------------------
# Schema definition
# --------------------------------------------------------------------------


class SchemaError(ChenDBError):
    """A schema definition is itself invalid (no columns, duplicate names, ...)."""
