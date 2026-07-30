"""The type system and its binary codecs.

Each SQL type owns a :class:`Codec` that converts between a Python value and
its on-disk bytes.  Codecs are looked up by :class:`DataType`, so adding a type
means adding one class and one registry entry. Nothing in the page, heap or
record layer changes.

Encoding
--------
======== ============ ======= ====================================
Type     Python        Bytes   Layout
======== ============ ======= ====================================
INTEGER  ``int``            8  ``<q`` two's-complement little-endian
FLOAT    ``float``          8  ``<d`` IEEE-754 binary64
BOOLEAN  ``bool``           1  ``0x00`` / ``0x01``
TEXT     ``str``       4 + n  ``<I`` byte length, then UTF-8
======== ============ ======= ====================================

Design notes
------------
**Fixed-width integers.** Every integer costs 8 bytes even when it holds 1.
SQLite instead stores integers in the narrowest of 1/2/3/4/6/8 bytes and
records the choice in a per-row header, which shrinks typical rows
substantially at the cost of a branch per column on both read and write.
PostgreSQL takes the third road: distinct declared types (``smallint``,
``integer``, ``bigint``) so width is a schema decision, resolved once.

**Little-endian.** Matches every platform this will realistically run on, so
encoding is a memory copy rather than a byte swap. Big-endian would let integer
keys be compared with ``memcmp``, genuinely useful for the B+ tree in
Milestone 5, and the reason key encodings in RocksDB and FoundationDB are
big-endian. ChenDB compares decoded values instead.

**4-byte text length.** Simple and uniform, but three bytes of overhead on a
short string. PostgreSQL uses a 1-byte header for values up to 126 bytes;
SQLite encodes lengths as varints. Both matter at scale. A table of short
strings pays roughly 3 bytes per row per column here.
"""

from __future__ import annotations

import math
import struct
from abc import ABC, abstractmethod
from enum import IntEnum
from typing import Any, ClassVar, Final

from engine.errors import SerializationError, TypeMismatchError

__all__ = [
    "INT64_MAX",
    "INT64_MIN",
    "BooleanCodec",
    "Codec",
    "DataType",
    "FloatCodec",
    "IntegerCodec",
    "TextCodec",
    "codec_for",
    "python_type_name",
]

_INT64: Final = struct.Struct("<q")
_FLOAT64: Final = struct.Struct("<d")
_UINT8: Final = struct.Struct("<B")
_UINT32: Final = struct.Struct("<I")

#: The range of an ``INTEGER``. Public because the expression evaluator needs
#: the same bound: an arithmetic result that does not fit is an error there
#: too, and two copies of a constant are two chances to disagree.
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1


class DataType(IntEnum):
    """A column's declared type. Values are persisted, so they are frozen."""

    INTEGER = 1
    FLOAT = 2
    BOOLEAN = 3
    TEXT = 4

    @property
    def sql_name(self) -> str:
        return _SQL_NAMES[self]

    @classmethod
    def from_sql_name(cls, name: str) -> DataType:
        """Resolve a SQL type name, accepting the common aliases."""
        try:
            return _SQL_ALIASES[name.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown data type {name!r}") from None


_SQL_NAMES: Final[dict[DataType, str]] = {
    DataType.INTEGER: "INTEGER",
    DataType.FLOAT: "FLOAT",
    DataType.BOOLEAN: "BOOLEAN",
    DataType.TEXT: "TEXT",
}

_SQL_ALIASES: Final[dict[str, DataType]] = {
    "INT": DataType.INTEGER,
    "INTEGER": DataType.INTEGER,
    "BIGINT": DataType.INTEGER,
    "FLOAT": DataType.FLOAT,
    "REAL": DataType.FLOAT,
    "DOUBLE": DataType.FLOAT,
    "BOOL": DataType.BOOLEAN,
    "BOOLEAN": DataType.BOOLEAN,
    "TEXT": DataType.TEXT,
    "VARCHAR": DataType.TEXT,
    "STRING": DataType.TEXT,
}


class Codec(ABC):
    """Converts one column value to and from bytes.

    ``fixed_size`` is the encoded width for fixed-width types and ``None`` for
    variable-width ones.  A schema whose columns are all fixed-width could have
    its field offsets precomputed; see :mod:`engine.serialization.record` for
    why Milestone 1 does not do that yet.
    """

    data_type: ClassVar[DataType]
    fixed_size: ClassVar[int | None]

    @abstractmethod
    def validate(self, value: Any) -> None:
        """Raise :class:`TypeMismatchError` if ``value`` cannot be encoded."""

    @abstractmethod
    def encode(self, value: Any) -> bytes:
        """Serialize a validated, non-``None`` value."""

    @abstractmethod
    def decode(self, buf: bytes, offset: int) -> tuple[Any, int]:
        """Deserialize at ``offset``; return the value and the next offset."""

    def encoded_size(self, value: Any) -> int:
        """Bytes ``value`` will occupy. Used for size estimates and planning."""
        return self.fixed_size if self.fixed_size is not None else len(self.encode(value))


def _require_bytes(buf: bytes, offset: int, size: int, type_name: str) -> None:
    """Bounds-check before unpacking.

    Without this, a truncated or mis-typed record surfaces as ``struct.error``,
    which is neither catchable through the engine's exception hierarchy nor
    informative about which column went wrong.
    """
    if offset + size > len(buf):
        raise SerializationError(
            f"{type_name} needs {size} bytes at offset {offset}, but the record "
            f"is only {len(buf)} bytes: it is truncated or does not match this schema"
        )


class IntegerCodec(Codec):
    """Signed 64-bit integer."""

    data_type: ClassVar[DataType] = DataType.INTEGER
    fixed_size: ClassVar[int | None] = 8

    def validate(self, value: Any) -> None:
        # bool is a subclass of int in Python, so `isinstance(True, int)` is
        # True. Accepting it here would let `True` round-trip as `1` and
        # silently change a value's type.
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeMismatchError(f"expected INTEGER, got {python_type_name(value)}")
        if not INT64_MIN <= value <= INT64_MAX:
            raise TypeMismatchError(
                f"integer {value} does not fit in 64 bits [{INT64_MIN}, {INT64_MAX}]"
            )

    def encode(self, value: Any) -> bytes:
        return _INT64.pack(value)

    def decode(self, buf: bytes, offset: int) -> tuple[int, int]:
        _require_bytes(buf, offset, 8, "INTEGER")
        return _INT64.unpack_from(buf, offset)[0], offset + 8


class FloatCodec(Codec):
    """IEEE-754 double, restricted to the finite ones.

    **NaN and the infinities are refused**, and that restriction is the whole
    interesting part of this codec. It is not squeamishness about odd values; it
    is that IEEE comparison is a *partial* order and every layer above here
    assumes a total one:

    * ``ORDER BY f`` returned ``2.0, NaN, 1.0, inf``, literally unsorted, because
      Python's sort compares with ``<`` and every comparison against NaN is
      false. A sort that silently does not sort is the worst thing in this list.
    * ``MIN``/``MAX`` seeded themselves with the first value and never displaced
      it, so the answer depended on insertion order.
    * :mod:`engine.index.key` orders NaN *above* ``+inf`` by its bit pattern,
      while the evaluator says ``NaN > 398.0`` is false. So an index scan and a
      sequential scan returned different rows for the same query, adding an
      index changed the answer.

    PostgreSQL keeps them and defines a total order instead, with NaN equal to
    itself and greater than everything. That is the more complete answer and it
    is four coordinated changes: comparison, sort, the aggregates, and the key
    encoding. Refusing costs one check and removes the whole class today, so it
    is what ChenDB does, and it mirrors ``INTEGER`` being exactly int64 rather
    than Python's unbounded integer. SQLite converts a non-finite result to NULL
    on store, which is a third answer and the only one that loses data silently.

    An arithmetic result is checked the same way, in
    :func:`~engine.executor.expression.check_numeric_range`, so ``1e308 * 10``
    cannot produce a value this column would reject.
    """

    data_type: ClassVar[DataType] = DataType.FLOAT
    fixed_size: ClassVar[int | None] = 8

    def validate(self, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeMismatchError(f"expected FLOAT, got {python_type_name(value)}")
        if not math.isfinite(value):
            raise TypeMismatchError(
                f"FLOAT cannot store {value}; NaN and infinity have no total "
                f"order, so a sort or an index over them would not be sorted"
            )

    def encode(self, value: Any) -> bytes:
        # Widening int to float here mirrors SQL, where `1` is a valid value
        # for a REAL column. The narrowing direction is never implicit.
        return _FLOAT64.pack(float(value))

    def decode(self, buf: bytes, offset: int) -> tuple[float, int]:
        _require_bytes(buf, offset, 8, "FLOAT")
        return _FLOAT64.unpack_from(buf, offset)[0], offset + 8


class BooleanCodec(Codec):
    """One byte, 0 or 1.

    A bit-packed representation would be denser, but the null bitmap already
    needs a byte per eight columns and mixing the two would complicate every
    offset calculation for a saving that only matters on very wide tables.
    """

    data_type: ClassVar[DataType] = DataType.BOOLEAN
    fixed_size: ClassVar[int | None] = 1

    def validate(self, value: Any) -> None:
        if not isinstance(value, bool):
            raise TypeMismatchError(f"expected BOOLEAN, got {python_type_name(value)}")

    def encode(self, value: Any) -> bytes:
        return _UINT8.pack(1 if value else 0)

    def decode(self, buf: bytes, offset: int) -> tuple[bool, int]:
        _require_bytes(buf, offset, 1, "BOOLEAN")
        return bool(_UINT8.unpack_from(buf, offset)[0]), offset + 1


class TextCodec(Codec):
    """UTF-8 string with a 4-byte length prefix."""

    data_type: ClassVar[DataType] = DataType.TEXT
    fixed_size: ClassVar[int | None] = None

    def validate(self, value: Any) -> None:
        if not isinstance(value, str):
            raise TypeMismatchError(f"expected TEXT, got {python_type_name(value)}")

    def encode(self, value: Any) -> bytes:
        payload = value.encode("utf-8")
        return _UINT32.pack(len(payload)) + payload

    def decode(self, buf: bytes, offset: int) -> tuple[str, int]:
        _require_bytes(buf, offset, 4, "TEXT length prefix")
        (length,) = _UINT32.unpack_from(buf, offset)
        start = offset + 4
        end = start + length
        if end > len(buf):
            raise SerializationError(
                f"TEXT claims {length} bytes at offset {start} but the record "
                f"is only {len(buf)} bytes"
            )
        try:
            return buf[start:end].decode("utf-8"), end
        except UnicodeDecodeError as exc:
            raise SerializationError(
                f"TEXT at offset {start} is not valid UTF-8: {exc}"
            ) from None

    def encoded_size(self, value: Any) -> int:
        return 4 + len(value.encode("utf-8"))


_CODECS: Final[dict[DataType, Codec]] = {
    DataType.INTEGER: IntegerCodec(),
    DataType.FLOAT: FloatCodec(),
    DataType.BOOLEAN: BooleanCodec(),
    DataType.TEXT: TextCodec(),
}


def codec_for(data_type: DataType) -> Codec:
    """Return the (stateless, shared) codec for ``data_type``."""
    try:
        return _CODECS[data_type]
    except KeyError:
        raise ValueError(f"no codec registered for {data_type!r}") from None


def python_type_name(value: Any) -> str:
    """Readable type name for error messages; ``None`` renders as ``NULL``."""
    return "NULL" if value is None else type(value).__name__
