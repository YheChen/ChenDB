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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from engine.errors import (
    NullConstraintViolation,
    SchemaMismatchError,
    SerializationError,
)
from engine.serialization.schema import Schema
from engine.serialization.types import codec_for

__all__ = [
    "FieldLayout",
    "RecordLayout",
    "Row",
    "decode_record",
    "describe_record",
    "encode_record",
    "estimate_record_size",
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
