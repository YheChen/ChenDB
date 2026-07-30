"""Order-preserving key encoding: making ``memcmp`` mean ``<``.

A B+ tree does one thing on every descent, every split and every scan: compare
two keys.  How keys are encoded therefore decides how fast the whole structure
is, and it is the single most consequential decision in this milestone.

The problem
-----------
:mod:`engine.serialization.record` encodes integers **little-endian** (``<q``),
because that matches every CPU this will run on and makes encoding a memory
copy.  But little-endian bytes do not sort::

    value   little-endian bytes        big-endian bytes
    -----   ------------------------   ------------------------
        1   01 00 00 00 00 00 00 00    00 00 00 00 00 00 00 01
      256   00 01 00 00 00 00 00 00    00 00 00 00 00 00 01 00

Comparing the little-endian forms byte by byte says ``1 > 256``, because the
first byte it looks at is the *least* significant one.  Two's complement makes
it worse: ``-1`` is ``ff ff ff ff ff ff ff ff``, which compares greater than
every positive number under an unsigned comparison.

Two ways out
------------
**(a) Decode both sides and compare Python values.**  Less code, no encoding
module at all.  But every comparison costs two ``struct.unpack`` calls plus a
Python-level ``<``.  A descent through a depth-3 tree with 200 entries per node
does ~24 comparisons, so a point lookup pays ~50 unpacks.

**(b) Encode keys so that byte order is value order.**  Comparison becomes a
single ``bytes`` comparison, which CPython dispatches to ``memcmp`` in C.  The
key is opaque to the tree: nothing in :mod:`engine.index.bplustree` knows or
cares what type it holds.

ChenDB takes **(b)**.  It is what RocksDB, FoundationDB and LevelDB do, for the
same reason, and it is why their APIs are byte-string-oriented.  The cost is
this module: one transform per type, each of which has to be exactly right.

The transforms
--------------
Every key is a **1-byte tag** followed by a payload.  The tag is what orders
``NULL`` against real values, and it gives internal nodes a minus-infinity
separator that no real value can collide with::

    0x00  minus infinity   (internal separators only, no payload)
    0x01  NULL             (no payload)
    0x02  a value          (payload follows)

======== ======================================================================
Type     Payload
======== ======================================================================
INTEGER  8 bytes big-endian of ``value + 2**63``.  Adding the bias flips the
         sign bit, mapping ``INT64_MIN → 0x0000…``, ``-1 → 0x7fff…``,
         ``0 → 0x8000…``, ``INT64_MAX → 0xffff…``: a monotonic map from signed
         to unsigned, so unsigned byte order *is* signed value order.
FLOAT    8 bytes big-endian of the IEEE-754 bit pattern, then: if the sign bit
         is set, flip every bit; otherwise flip only the sign bit.  IEEE-754 was
         designed so that the bit patterns of positive floats sort correctly as
         integers; the transform extends that to negatives, whose magnitude
         ordering runs backwards.
BOOLEAN  1 byte, ``0x00`` or ``0x01``.  Already ordered.
TEXT     Raw UTF-8, no length prefix.  UTF-8's defining property is that byte
         order equals code-point order, so nothing needs doing.  A length
         prefix would be a disaster here: it would sort ``"z"`` before ``"aa"``.
======== ======================================================================

Ordering is *binary*, not linguistic: ``"Z" < "a"`` because ``0x5a < 0x61``.
Real collation (``ORDER BY name COLLATE "en_US"``) is a much larger problem,
PostgreSQL delegates it to the operating system's ICU or libc, and a glibc
upgrade that changed collation famously silently corrupted people's indexes.
Binary ordering has the compensating virtue of never changing.

Why single-column keys
----------------------
A composite key would concatenate two encodings, and concatenation breaks the
prefix property: ``"ab" ‖ "c"`` and ``"a" ‖ "bc"`` produce identical bytes.
Fixing it needs an escape-and-terminate layer, replace ``0x00`` with
``0x00 0xff`` and terminate with ``0x00 0x00``, which is exactly what
FoundationDB's tuple layer does.  Milestone 5 indexes one column, so that layer
is not needed and is not written.  The single place it *would* be needed is
handled structurally instead: see :mod:`engine.index.node` on why the record id
suffix is split off by length rather than compared as part of the key bytes.
"""

from __future__ import annotations

import struct
from typing import Any, Final

from engine.errors import IndexingError, SerializationError
from engine.serialization.types import DataType

__all__ = [
    "MINUS_INFINITY",
    "SMALLEST_VALUE_KEY",
    "TAG_MINUS_INFINITY",
    "TAG_NULL",
    "TAG_VALUE",
    "decode_key",
    "describe_key",
    "encode_key",
    "is_minus_infinity",
    "is_null_key",
]

TAG_MINUS_INFINITY: Final = 0x00
TAG_NULL: Final = 0x01
TAG_VALUE: Final = 0x02

#: The separator on the first entry of every internal node.  Sorts below every
#: real key, including ``NULL``, so a descent always finds a child to follow.
MINUS_INFINITY: Final[bytes] = bytes([TAG_MINUS_INFINITY])

#: A ``NULL`` key.  Sorts above minus infinity and below every value, which is
#: SQLite's ordering and PostgreSQL's ``NULLS FIRST``.  PostgreSQL's *default*
#: for ``ASC`` is ``NULLS LAST``; that would need the tag to be ``0xff``, and
#: an index would then need to know its own null ordering to be searchable.
_NULL_KEY: Final[bytes] = bytes([TAG_NULL])

_VALUE_PREFIX: Final[bytes] = bytes([TAG_VALUE])

#: The lowest key any non-``NULL`` value can encode to, and therefore the lower
#: bound a range scan uses when the query bounds only the top: ``x < 5`` must not
#: return rows where ``x`` is ``NULL``, but an unbounded scan would sweep the
#: NULL keys up because they sort below every value.  Anchoring here excludes
#: them, and it is inclusive because ``encode_key("", TEXT)`` is exactly this.
SMALLEST_VALUE_KEY: Final[bytes] = _VALUE_PREFIX

_INT_BIAS: Final = 2**63
_U64_BE: Final = struct.Struct(">Q")
_F64_BE: Final = struct.Struct(">d")
_SIGN_BIT: Final = 0x8000_0000_0000_0000
_ALL_ONES: Final = 0xFFFF_FFFF_FFFF_FFFF


def encode_key(value: Any, data_type: DataType) -> bytes:
    """Encode one column value into its order-preserving form.

    ``None`` becomes the NULL key.  The caller is expected to have validated the
    value against the column's type already; a mismatch raises here rather than
    producing a key that sorts nonsensically.
    """
    if value is None:
        return _NULL_KEY

    match data_type:
        case DataType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise IndexingError(f"index key must be an INTEGER, got {value!r}")
            try:
                return _VALUE_PREFIX + _U64_BE.pack(value + _INT_BIAS)
            except struct.error:
                raise IndexingError(
                    f"integer {value} does not fit in 64 bits and cannot be indexed"
                ) from None

        case DataType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise IndexingError(f"index key must be a FLOAT, got {value!r}")
            return _VALUE_PREFIX + _U64_BE.pack(_float_to_sortable(float(value)))

        case DataType.BOOLEAN:
            if not isinstance(value, bool):
                raise IndexingError(f"index key must be a BOOLEAN, got {value!r}")
            return _VALUE_PREFIX + (b"\x01" if value else b"\x00")

        case DataType.TEXT:
            if not isinstance(value, str):
                raise IndexingError(f"index key must be TEXT, got {value!r}")
            return _VALUE_PREFIX + value.encode("utf-8")

    raise IndexingError(f"no index key encoding for {data_type!r}")


def decode_key(key: bytes, data_type: DataType) -> Any:
    """Recover the Python value from an encoded key.

    Needed by the visualizer (a tree of hex blobs teaches nothing) and by
    range scans that report their bounds.  Never on the hot comparison path.
    """
    if not key:
        raise SerializationError("index key is empty")
    tag = key[0]
    if tag == TAG_MINUS_INFINITY:
        raise SerializationError("minus infinity is a separator, not a value")
    if tag == TAG_NULL:
        return None
    if tag != TAG_VALUE:
        raise SerializationError(f"unknown index key tag 0x{tag:02x}")

    body = key[1:]
    match data_type:
        case DataType.INTEGER:
            _require(body, 8, "INTEGER")
            return _U64_BE.unpack(body)[0] - _INT_BIAS
        case DataType.FLOAT:
            _require(body, 8, "FLOAT")
            return _F64_BE.unpack(
                _U64_BE.pack(_sortable_to_float_bits(_U64_BE.unpack(body)[0]))
            )[0]
        case DataType.BOOLEAN:
            _require(body, 1, "BOOLEAN")
            return body[0] != 0
        case DataType.TEXT:
            try:
                return body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SerializationError(f"index key is not valid UTF-8: {exc}") from None

    raise SerializationError(f"no index key decoding for {data_type!r}")


def describe_key(key: bytes, data_type: DataType) -> str:
    """Render a key for events, plan text and the tree view.

    Falls back to hex rather than raising: diagnostics must never be the thing
    that breaks, and a corrupt key is exactly when you most want to see it.
    """
    if is_minus_infinity(key):
        return "-∞"
    try:
        value = decode_key(key, data_type)
    except (SerializationError, struct.error):
        return f"0x{key.hex()}"
    if value is None:
        return "NULL"
    return repr(value) if isinstance(value, str) else str(value)


def is_minus_infinity(key: bytes) -> bool:
    return len(key) == 1 and key[0] == TAG_MINUS_INFINITY


def is_null_key(key: bytes) -> bool:
    """Whether ``key`` encodes SQL ``NULL``.

    Load-bearing for unique indexes: SQL allows any number of NULLs in a unique
    column, because two unknowns are not known to be equal.
    """
    return len(key) == 1 and key[0] == TAG_NULL


# -- float ordering --------------------------------------------------------


def _float_to_sortable(value: float) -> int:
    """Map an IEEE-754 double onto a uint64 whose order matches ``<``.

    Negatives get every bit flipped, which both moves them below the positives
    and reverses their magnitude ordering (``-1.0 < -0.5`` but
    ``|-1.0| > |-0.5|``).  Positives get only the sign bit set, lifting them
    above the negatives.

    ``-0.0`` and ``0.0`` therefore encode differently even though they compare
    equal in Python.  That is a genuine wart: an index would treat them as two
    keys.  PostgreSQL normalises ``-0.0`` to ``0.0`` on input to dodge it, and
    so does this function.  ``NaN`` has the sign bit clear in its usual
    encoding, so it sorts above every real number, matching PostgreSQL, which
    documents ``NaN`` as greater than all other float values.
    """
    if value == 0.0:
        value = 0.0  # collapses -0.0, which would otherwise encode below 0.0
    bits = _U64_BE.unpack(_F64_BE.pack(value))[0]
    return bits ^ _ALL_ONES if bits & _SIGN_BIT else bits | _SIGN_BIT


def _sortable_to_float_bits(sortable: int) -> int:
    """Inverse of :func:`_float_to_sortable`, back to raw IEEE-754 bits."""
    return sortable & ~_SIGN_BIT if sortable & _SIGN_BIT else sortable ^ _ALL_ONES


def _require(body: bytes, size: int, type_name: str) -> None:
    if len(body) != size:
        raise SerializationError(
            f"{type_name} index key should be {size} bytes, got {len(body)}"
        )
