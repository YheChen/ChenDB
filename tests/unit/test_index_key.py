"""The order-preserving key encoding.

Every one of these tests is really the same assertion: **sorting the encoded
bytes must give the same order as sorting the Python values**.  That property is
what the whole B+ tree rests on, and it is the one that little-endian record
encoding does *not* have, so it is worth checking exhaustively rather than
sampling, especially around the sign boundary and IEEE-754's special values.
"""

from __future__ import annotations

import math
import struct

import pytest

from engine.errors import IndexingError, SerializationError
from engine.index.key import (
    MINUS_INFINITY,
    SMALLEST_VALUE_KEY,
    decode_key,
    describe_key,
    encode_key,
    is_minus_infinity,
    is_null_key,
)
from engine.serialization.types import DataType

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def keys(values, data_type):
    return [encode_key(value, data_type) for value in values]


# -- the property that matters ---------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "values"),
    [
        (
            DataType.INTEGER,
            [
                INT64_MIN,
                -(2**40),
                -70000,
                -257,
                -256,
                -255,
                -1,
                0,
                1,
                255,
                256,
                2**40,
                INT64_MAX,
            ],
        ),
        (
            DataType.FLOAT,
            [
                -math.inf,
                -1e308,
                -1.5,
                -1e-308,
                0.0,
                1e-308,
                0.5,
                1.5,
                1e308,
                math.inf,
            ],
        ),
        (DataType.BOOLEAN, [False, True]),
        (
            DataType.TEXT,
            ["", "A", "Z", "a", "aa", "ab", "b", "z", "za", "é", "😀"],
        ),
    ],
)
def test_encoded_order_matches_value_order(data_type: DataType, values: list):
    encoded = keys(values, data_type)
    assert encoded == sorted(encoded), f"{data_type.sql_name} keys do not sort by memcmp"


@pytest.mark.parametrize(
    ("data_type", "values"),
    [
        (DataType.INTEGER, [INT64_MIN, -1, 0, 1, 42, INT64_MAX]),
        (DataType.FLOAT, [-math.inf, -1.5, 0.0, 0.5, 1e300, math.inf]),
        (DataType.BOOLEAN, [False, True]),
        (DataType.TEXT, ["", "hello", "a b\tc", "é😀"]),
    ],
)
def test_round_trip(data_type: DataType, values: list):
    for value in values:
        assert decode_key(encode_key(value, data_type), data_type) == value


def test_the_little_endian_record_encoding_would_not_have_worked():
    # The exact failure this module exists to avoid: 1 sorting above 256.
    assert struct.pack("<q", 1) > struct.pack("<q", 256)
    assert encode_key(1, DataType.INTEGER) < encode_key(256, DataType.INTEGER)


def test_negative_integers_sort_below_positive_ones():
    # Two's complement makes -1 all-ones, which is the largest unsigned value.
    # The bias flip is what fixes it.
    assert struct.pack(">q", -1) > struct.pack(">q", 1)
    assert encode_key(-1, DataType.INTEGER) < encode_key(1, DataType.INTEGER)


def test_a_dense_run_of_integers_sorts_exactly():
    values = list(range(-600, 600))
    assert keys(values, DataType.INTEGER) == sorted(keys(values, DataType.INTEGER))


# -- tags: minus infinity, NULL, values ------------------------------------


def test_the_three_tags_sort_in_order():
    assert encode_key(None, DataType.INTEGER) > MINUS_INFINITY
    assert encode_key(None, DataType.INTEGER) < encode_key(INT64_MIN, DataType.INTEGER)


def test_null_sorts_below_every_value_of_every_type():
    null = encode_key(None, DataType.TEXT)
    for data_type, value in [
        (DataType.INTEGER, INT64_MIN),
        (DataType.FLOAT, -math.inf),
        (DataType.BOOLEAN, False),
        (DataType.TEXT, ""),
    ]:
        assert null < encode_key(value, data_type)


def test_the_smallest_value_key_excludes_nulls_and_admits_everything_else():
    # This is what a range scan bounded only from above uses as its low bound,
    # so `x < 5` does not return rows where x is NULL.
    assert encode_key(None, DataType.INTEGER) < SMALLEST_VALUE_KEY
    assert encode_key(INT64_MIN, DataType.INTEGER) >= SMALLEST_VALUE_KEY
    assert encode_key("", DataType.TEXT) == SMALLEST_VALUE_KEY


def test_tag_predicates():
    assert is_minus_infinity(MINUS_INFINITY)
    assert not is_minus_infinity(encode_key(0, DataType.INTEGER))
    assert is_null_key(encode_key(None, DataType.TEXT))
    assert not is_null_key(encode_key("", DataType.TEXT))


def test_minus_infinity_cannot_be_decoded_as_a_value():
    with pytest.raises(SerializationError, match="separator"):
        decode_key(MINUS_INFINITY, DataType.INTEGER)


# -- float corners ----------------------------------------------------------


def test_negative_zero_encodes_as_positive_zero():
    # Otherwise -0.0 and 0.0 would be two distinct keys for a value that compares
    # equal, and a unique index would accept both.
    assert encode_key(-0.0, DataType.FLOAT) == encode_key(0.0, DataType.FLOAT)


def test_nan_sorts_above_every_real_number():
    # Matches PostgreSQL, which documents NaN as greater than all other floats so
    # that a total order exists at all.
    nan = encode_key(math.nan, DataType.FLOAT)
    assert nan > encode_key(math.inf, DataType.FLOAT)


def test_infinities_bracket_the_finite_range():
    low = encode_key(-math.inf, DataType.FLOAT)
    high = encode_key(math.inf, DataType.FLOAT)
    for value in (-1e308, -1.0, 0.0, 1.0, 1e308):
        assert low < encode_key(value, DataType.FLOAT) < high


def test_an_integer_is_accepted_for_a_float_column():
    # SQL widens: `1` is a valid REAL. The narrowing direction is not implicit.
    assert encode_key(1, DataType.FLOAT) == encode_key(1.0, DataType.FLOAT)


# -- text -------------------------------------------------------------------


def test_text_has_no_length_prefix():
    # A length prefix would sort "z" before "aa", which is the classic mistake.
    assert encode_key("z", DataType.TEXT) > encode_key("aa", DataType.TEXT)


def test_a_prefix_sorts_before_the_string_extending_it():
    assert encode_key("ab", DataType.TEXT) < encode_key("abc", DataType.TEXT)


def test_ordering_is_binary_not_linguistic():
    # 'Z' is 0x5a and 'a' is 0x61. Real collation is an operating-system problem;
    # binary ordering at least never changes under you.
    assert encode_key("Z", DataType.TEXT) < encode_key("a", DataType.TEXT)


def test_embedded_nul_round_trips():
    assert decode_key(encode_key("a\x00b", DataType.TEXT), DataType.TEXT) == "a\x00b"


# -- errors and rendering ---------------------------------------------------


@pytest.mark.parametrize(
    ("value", "data_type"),
    [
        ("nope", DataType.INTEGER),
        (1.5, DataType.INTEGER),
        (True, DataType.INTEGER),
        (1, DataType.BOOLEAN),
        (b"raw", DataType.TEXT),
        (1, DataType.TEXT),
        (True, DataType.FLOAT),
    ],
)
def test_a_mismatched_value_is_refused(value, data_type: DataType):
    # The planner relies on this: it tries to encode a literal and falls back to
    # a sequential scan when the type does not fit, rather than guessing.
    with pytest.raises(IndexingError):
        encode_key(value, data_type)


def test_an_integer_too_large_for_64_bits_is_refused():
    with pytest.raises(IndexingError, match="64 bits"):
        encode_key(2**64, DataType.INTEGER)


def test_a_truncated_key_is_reported_not_unpacked():
    with pytest.raises(SerializationError, match="8 bytes"):
        decode_key(b"\x02\x00\x00", DataType.INTEGER)


def test_an_unknown_tag_is_reported():
    with pytest.raises(SerializationError, match="tag"):
        decode_key(b"\x7f\x00", DataType.INTEGER)


def test_describe_renders_values_for_display():
    assert describe_key(MINUS_INFINITY, DataType.INTEGER) == "-∞"
    assert describe_key(encode_key(None, DataType.INTEGER), DataType.INTEGER) == "NULL"
    assert describe_key(encode_key(42, DataType.INTEGER), DataType.INTEGER) == "42"
    assert describe_key(encode_key("hi", DataType.TEXT), DataType.TEXT) == "'hi'"


def test_describe_falls_back_to_hex_rather_than_raising():
    # Diagnostics must never be the thing that breaks, and a corrupt key is
    # exactly when you most want to see its bytes.
    assert describe_key(b"\x02\xff", DataType.INTEGER) == "0x02ff"
