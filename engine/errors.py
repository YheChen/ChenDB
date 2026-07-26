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


# --------------------------------------------------------------------------
# SQL front end
# --------------------------------------------------------------------------


class SqlError(ChenDBError):
    """Base class for failures in the SQL front end.

    Every subclass carries the source position that caused it, so a caller can
    point at the offending characters rather than just reporting "syntax error".
    """

    def __init__(
        self,
        message: str,
        *,
        start: int = 0,
        end: int = 0,
        line: int = 1,
        column: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.start = start
        self.end = end
        self.line = line
        self.column = column

    def __str__(self) -> str:
        return f"{self.message} (line {self.line}, column {self.column})"


class LexError(SqlError):
    """The tokenizer could not turn the input into tokens.

    An unterminated string, an unknown character, a malformed number.
    """


class ParseError(SqlError):
    """The tokens do not form a valid statement."""

    def __init__(
        self,
        message: str,
        *,
        start: int = 0,
        end: int = 0,
        line: int = 1,
        column: int = 1,
        expected: tuple[str, ...] = (),
        found: str = "",
    ) -> None:
        super().__init__(message, start=start, end=end, line=line, column=column)
        self.expected = expected
        self.found = found


class UnsupportedSqlError(ParseError):
    """Valid SQL that this milestone does not implement yet.

    Distinct from :class:`ParseError` on purpose: "you wrote this wrong" and
    "ChenDB cannot do this yet" are different messages, and only the second one
    should point at a milestone.
    """


# --------------------------------------------------------------------------
# Binding and execution
# --------------------------------------------------------------------------


class BindingError(SqlError):
    """A statement is syntactically valid but does not match the schema.

    An unknown table or column, a projection of a column that does not exist, an
    ``INSERT`` naming a column twice.  Carries a source position, so the editor
    can point at the offending identifier rather than the whole statement.
    """


class ExecutionError(ChenDBError):
    """A query failed while running."""


class EvaluationError(ExecutionError):
    """An expression could not be evaluated.

    A type mismatch the binder could not catch statically, or division by zero.
    """


class QueryCancelledError(ExecutionError):
    """Execution was cancelled by its controller.

    Raised inside the engine thread at the next checkpoint so operators unwind
    through their normal ``close()`` path rather than being abandoned.
    """
