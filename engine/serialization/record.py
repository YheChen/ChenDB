"""Turning rows of Python values into bytes and back.

Record layout::

    ┌──────────────┬─────────┬─────────┬─────┬─────────┐
    │ null bitmap  │ value 0 │ value 1 │ ... │ value n │
    │  ⌈cols/8⌉ B  │         │         │     │         │
    └──────────────┴─────────┴─────────┴─────┴─────────┘

Bit *i* of the bitmap is set when column *i* is NULL, and a NULL column
contributes **no** bytes to the value area.  A row of five NULLs therefore
costs one byte.

Two decisions worth naming
--------------------------
**The bitmap is always present.** PostgreSQL omits it entirely when a tuple has
no NULLs, flagging that in ``t_infomask``; on a NOT NULL table that saves a
byte per row plus alignment padding. ChenDB always writes it, trading a byte
for a branch-free decoder.

**Values are laid out sequentially with no alignment padding.** Reading column
*k* means walking columns 0..k-1, which is O(k). That is fine for a scan, which
decodes whole rows anyway, but it means a projection of the last column of a
wide table still pays to walk the row. PostgreSQL caches per-attribute offsets
(``attcacheoff``) for the fixed-width prefix of a tuple so those columns are
O(1); SQLite stores every field's type-and-length in a header at the front of
the record, so it can skip ahead without decoding values. ChenDB will need one
of these if wide tables ever become a target.

Complexity
----------
Encode and decode are both O(row bytes). Neither allocates beyond the result.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from engine.errors import (
    NullConstraintViolation,
    SchemaMismatchError,
    SerializationError,
)
from engine.serialization.schema import Schema
from engine.serialization.types import codec_for

__all__ = [
    "NO_TRANSACTION_ID",
    "TUPLE_HEADER_SIZE",
    "FieldLayout",
    "RecordLayout",
    "Row",
    "TupleHeader",
    "add_tuple_header",
    "decode_record",
    "describe_record",
    "encode_record",
    "estimate_record_size",
    "read_tuple_header",
    "set_xmax",
    "strip_tuple_header",
]

#: A row is a positional tuple of Python values matching a schema's columns.
Row = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class FieldLayout:
    """Where one column lives inside an encoded record.

    Produced by :func:`describe_record` for the page inspector, which shows the
    byte range each column occupies next to the raw hex.
    """

    index: int
    name: str
    type_name: str
    is_null: bool
    offset: int
    """Byte offset within the record. ``-1`` for a NULL, which occupies nothing."""
    length: int
    value: Any


@dataclass(frozen=True, slots=True)
class RecordLayout:
    """A fully decoded record together with its physical layout."""

    values: Row
    fields: tuple[FieldLayout, ...]
    null_bitmap: bytes
    null_bitmap_size: int
    total_size: int


def encode_record(schema: Schema, values: Sequence[Any]) -> bytes:
    """Serialize ``values`` according to ``schema``.

    Raises :class:`SchemaMismatchError` on an arity mismatch,
    :class:`NullConstraintViolation` for a NULL in a NOT NULL column, and
    :class:`~engine.errors.TypeMismatchError` for a wrong Python type.
    Validation happens here, at the boundary, so nothing invalid ever reaches
    a page.
    """
    if len(values) != len(schema.columns):
        raise SchemaMismatchError(
            f"row has {len(values)} values but table has {len(schema.columns)} columns"
        )

    bitmap = bytearray(schema.null_bitmap_size)
    parts: list[bytes] = []
    for index, (column, value) in enumerate(zip(schema.columns, values, strict=True)):
        if value is None:
            if not column.nullable:
                raise NullConstraintViolation(
                    f"column {column.name!r} is NOT NULL but received NULL"
                )
            bitmap[index // 8] |= 1 << (index % 8)
            continue
        codec = codec_for(column.data_type)
        try:
            codec.validate(value)
        except SerializationError as exc:
            raise type(exc)(f"column {column.name!r}: {exc}") from None
        parts.append(codec.encode(value))

    return bytes(bitmap) + b"".join(parts)


def decode_record(schema: Schema, raw: bytes) -> Row:
    """Deserialize ``raw`` into a tuple of Python values."""
    bitmap_size = schema.null_bitmap_size
    if len(raw) < bitmap_size:
        raise SerializationError(
            f"record is {len(raw)} bytes, too short for a {bitmap_size}-byte null bitmap"
        )

    offset = bitmap_size
    values: list[Any] = []
    for index, column in enumerate(schema.columns):
        if _is_null(raw, index):
            values.append(None)
            continue
        value, offset = codec_for(column.data_type).decode(raw, offset)
        values.append(value)

    if offset != len(raw):
        raise SerializationError(
            f"record has {len(raw) - offset} trailing bytes after decoding "
            f"{len(schema.columns)} columns; the schema does not match this data"
        )
    return tuple(values)


def describe_record(schema: Schema, raw: bytes) -> RecordLayout:
    """Decode ``raw`` and report where each column physically sits.

    Same work as :func:`decode_record` plus per-field bookkeeping. The
    inspector uses it; the scan path does not, so the extra allocations stay
    off the hot path.
    """
    bitmap_size = schema.null_bitmap_size
    if len(raw) < bitmap_size:
        raise SerializationError(
            f"record is {len(raw)} bytes, too short for a {bitmap_size}-byte null bitmap"
        )

    offset = bitmap_size
    fields: list[FieldLayout] = []
    values: list[Any] = []
    for index, column in enumerate(schema.columns):
        if _is_null(raw, index):
            values.append(None)
            fields.append(
                FieldLayout(
                    index=index,
                    name=column.name,
                    type_name=column.data_type.sql_name,
                    is_null=True,
                    offset=-1,
                    length=0,
                    value=None,
                )
            )
            continue
        start = offset
        value, offset = codec_for(column.data_type).decode(raw, offset)
        values.append(value)
        fields.append(
            FieldLayout(
                index=index,
                name=column.name,
                type_name=column.data_type.sql_name,
                is_null=False,
                offset=start,
                length=offset - start,
                value=value,
            )
        )

    return RecordLayout(
        values=tuple(values),
        fields=tuple(fields),
        null_bitmap=bytes(raw[:bitmap_size]),
        null_bitmap_size=bitmap_size,
        total_size=len(raw),
    )


def estimate_record_size(schema: Schema, values: Sequence[Any]) -> int:
    """Encoded size of a row without building the bytes.

    Used by the planner's cost model from Milestone 6, and by tests that assert
    the layout has not drifted.
    """
    size = schema.null_bitmap_size
    for column, value in zip(schema.columns, values, strict=True):
        if value is None:
            continue
        size += codec_for(column.data_type).encoded_size(value)
    return size


def _is_null(raw: bytes, index: int) -> bool:
    return bool(raw[index // 8] >> (index % 8) & 1)


# -- tuple headers (Milestone 10) ------------------------------------------
#
#     ┌──────┬──────┬──────────────┬─────────┬─────┐
#     │ xmin │ xmax │ null bitmap  │ value 0 │ ... │
#     │ u32  │ u32  │  ⌈cols/8⌉ B  │         │     │
#     └──────┴──────┴──────────────┴─────────┴─────┘
#
# Eight bytes per row saying which transaction created it and which one deleted
# it. That is what makes a row a *version* rather than a value, and it is the
# whole of MVCC's storage cost: 8 bytes against PostgreSQL's 23-byte
# ``HeapTupleHeaderData``, which also carries a command id, a ctid forward
# pointer to the next version, and two infomask words of cached flags.
#
# On a thirty-byte row that is a 27% overhead, paid by every row whether or not
# anything ever reads concurrently. It buys the thing that cannot be bought any
# other way: **a reader never blocks a writer**, because it reads an older
# version rather than waiting for the newer one.
#
# **32-bit transaction ids, deliberately.** PostgreSQL uses 32 bits too, and it
# is the source of one of its most famous operational hazards: after four
# billion transactions the counter wraps, and a row's ``xmin`` starts looking
# like it is in the *future*. PostgreSQL handles that with a circular comparison
# and an anti-wraparound VACUUM that must freeze old rows before the wrap
# catches them: a maintenance job that has taken production systems down.
# ChenDB cannot hit it for a reason worth understanding, spelled out in
# ``engine/concurrency/snapshot.py``: rollback here physically removes a
# transaction's work, so every row that survives is from a committed
# transaction, and a single number in the meta page can declare everything below
# it frozen.

TUPLE_HEADER_FORMAT: Final[str] = "<II"
TUPLE_HEADER_SIZE: Final[int] = struct.calcsize(TUPLE_HEADER_FORMAT)  # 8

#: ``xmax`` for a row nobody has deleted. Zero is safe as a sentinel because
#: transaction ids start at 1, the same reason ``INVALID_PAGE_ID`` is not 0.
NO_TRANSACTION_ID: Final = 0


@dataclass(frozen=True, slots=True)
class TupleHeader:
    """Which transactions created and deleted a row version."""

    xmin: int
    """The transaction that inserted this version."""
    xmax: int
    """The transaction that deleted it, or 0 if none has."""

    @property
    def deleted(self) -> bool:
        return self.xmax != NO_TRANSACTION_ID


def add_tuple_header(payload: bytes, xmin: int, xmax: int = NO_TRANSACTION_ID) -> bytes:
    """Prefix an encoded record with its version header."""
    return struct.pack(TUPLE_HEADER_FORMAT, xmin, xmax) + payload


def read_tuple_header(raw: bytes) -> TupleHeader:
    """Decode the header without touching the row.

    Called once per row per scan, before the visibility check decides whether
    the row is worth decoding at all, which is the point of putting the header
    first. A row that is invisible costs eight bytes of unpacking rather than a
    walk of every column.
    """
    if len(raw) < TUPLE_HEADER_SIZE:
        raise SerializationError(
            f"record is {len(raw)} bytes, too short for an "
            f"{TUPLE_HEADER_SIZE}-byte tuple header"
        )
    xmin, xmax = struct.unpack_from(TUPLE_HEADER_FORMAT, raw, 0)
    return TupleHeader(xmin=xmin, xmax=xmax)


def strip_tuple_header(raw: bytes) -> bytes:
    """The encoded row, without its header."""
    return raw[TUPLE_HEADER_SIZE:]


def set_xmax(raw: bytes, xmax: int) -> bytes:
    """Mark a version deleted, returning the new bytes.

    A delete rewrites eight bytes in place rather than removing the row, which
    is what lets a transaction that started earlier keep reading it. The slot is
    reclaimed later, by :meth:`Database.vacuum`, which is the price of never
    blocking a reader, and the reason PostgreSQL ships an autovacuum daemon.
    """
    buf = bytearray(raw)
    struct.pack_into("<I", buf, 4, xmax)
    return bytes(buf)
