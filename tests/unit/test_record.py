"""Record encoding, the null bitmap, and the type system."""

from __future__ import annotations

import math

import pytest

from engine.errors import (
    NullConstraintViolation,
    SchemaError,
    SchemaMismatchError,
    SerializationError,
    TypeMismatchError,
)
from engine.serialization.record import (
    decode_record,
    describe_record,
    encode_record,
    estimate_record_size,
)
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType, codec_for


@pytest.fixture
def schema() -> Schema:
    return Schema.of(
        Column("id", DataType.INTEGER, nullable=False),
        Column("name", DataType.TEXT),
        Column("ratio", DataType.FLOAT),
        Column("flag", DataType.BOOLEAN),
    )


# -- roundtrips ------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        (1, "Ada", 1.5, True),
        (0, "", 0.0, False),
        (-(2**63), "x" * 500, -1.5e300, True),
        (2**63 - 1, "unicode: héllo 世界 🎉", 1e-300, False),
        (7, None, None, None),
    ],
)
def test_encode_decode_roundtrip(schema: Schema, row: tuple):
    assert decode_record(schema, encode_record(schema, row)) == row


def test_all_null_row_costs_only_the_bitmap(schema: Schema):
    nullable = Schema.of(
        Column("a", DataType.INTEGER),
        Column("b", DataType.TEXT),
        Column("c", DataType.FLOAT),
        Column("d", DataType.BOOLEAN),
    )
    raw = encode_record(nullable, (None, None, None, None))
    assert len(raw) == nullable.null_bitmap_size == 1
    assert decode_record(nullable, raw) == (None, None, None, None)


def test_null_bitmap_marks_exactly_the_null_columns(schema: Schema):
    raw = encode_record(schema, (5, None, 2.0, None))
    # bit 1 (name) and bit 3 (flag) set -> 0b1010 == 0x0a
    assert raw[0] == 0b0000_1010


def test_bitmap_grows_one_byte_per_eight_columns():
    for column_count in (1, 8, 9, 16, 17, 64):
        schema = Schema(
            tuple(Column(f"c{i}", DataType.INTEGER) for i in range(column_count))
        )
        expected = (column_count + 7) // 8
        assert schema.null_bitmap_size == expected
        row = tuple([None] * column_count)
        assert len(encode_record(schema, row)) == expected


def test_ninth_column_null_flips_a_bit_in_the_second_byte():
    schema = Schema(tuple(Column(f"c{i}", DataType.INTEGER) for i in range(10)))
    raw = encode_record(schema, tuple(0 if i != 8 else None for i in range(10)))
    assert raw[0] == 0
    assert raw[1] == 0b0000_0001


def test_float_special_values_survive():
    schema = Schema.of(Column("x", DataType.FLOAT))
    for value in (float("inf"), float("-inf"), -0.0):
        assert decode_record(schema, encode_record(schema, (value,))) == (value,)
    (nan,) = decode_record(schema, encode_record(schema, (float("nan"),)))
    assert math.isnan(nan)


# -- validation ------------------------------------------------------------


def test_wrong_arity_is_rejected(schema: Schema):
    with pytest.raises(SchemaMismatchError, match="4 columns"):
        encode_record(schema, (1, "a", 1.0))


def test_not_null_column_rejects_none(schema: Schema):
    with pytest.raises(NullConstraintViolation, match="'id'"):
        encode_record(schema, (None, "a", 1.0, True))


def test_type_errors_name_the_offending_column(schema: Schema):
    with pytest.raises(TypeMismatchError, match="'id'"):
        encode_record(schema, ("not an int", "a", 1.0, True))


def test_bool_is_not_accepted_as_an_integer():
    # bool subclasses int in Python; accepting True here would silently store
    # it as 1 and change the value's type on the way back out.
    schema = Schema.of(Column("n", DataType.INTEGER))
    with pytest.raises(TypeMismatchError, match="expected INTEGER"):
        encode_record(schema, (True,))


def test_integer_out_of_64_bit_range_is_rejected():
    schema = Schema.of(Column("n", DataType.INTEGER))
    with pytest.raises(TypeMismatchError, match="64 bits"):
        encode_record(schema, (2**63,))


def test_int_widens_to_float_but_float_never_narrows_to_int():
    float_schema = Schema.of(Column("x", DataType.FLOAT))
    assert decode_record(float_schema, encode_record(float_schema, (3,))) == (3.0,)

    int_schema = Schema.of(Column("x", DataType.INTEGER))
    with pytest.raises(TypeMismatchError):
        encode_record(int_schema, (3.0,))


def test_trailing_bytes_mean_the_schema_does_not_match_the_data(schema: Schema):
    raw = encode_record(schema, (1, "a", 1.0, True))
    with pytest.raises(SerializationError, match="trailing bytes"):
        decode_record(schema, raw + b"\x00")


def test_truncated_record_is_rejected(schema: Schema):
    raw = encode_record(schema, (1, "abc", 1.0, True))
    with pytest.raises(SerializationError):
        decode_record(schema, raw[:-3])


def test_record_shorter_than_the_bitmap_is_rejected(schema: Schema):
    with pytest.raises(SerializationError, match="null bitmap"):
        decode_record(schema, b"")


# -- layout description ----------------------------------------------------


def test_describe_record_reports_byte_ranges(schema: Schema):
    layout = describe_record(schema, encode_record(schema, (1, "abc", 2.5, True)))

    assert layout.values == (1, "abc", 2.5, True)
    assert layout.null_bitmap_size == 1
    assert [(f.name, f.offset, f.length) for f in layout.fields] == [
        ("id", 1, 8),
        ("name", 9, 7),  # 4-byte length prefix + 3 bytes of UTF-8
        ("ratio", 16, 8),
        ("flag", 24, 1),
    ]
    assert layout.total_size == 25


def test_describe_record_marks_nulls_with_no_storage(schema: Schema):
    layout = describe_record(schema, encode_record(schema, (1, None, None, None)))
    name_field = layout.fields[1]
    assert name_field.is_null
    assert name_field.offset == -1
    assert name_field.length == 0


def test_estimate_matches_the_real_encoded_size(schema: Schema):
    for row in [(1, "abc", 2.5, True), (2, None, None, False), (3, "x" * 100, 0.0, True)]:
        assert estimate_record_size(schema, row) == len(encode_record(schema, row))


# -- type system -----------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (DataType.INTEGER, 8),
        (DataType.FLOAT, 8),
        (DataType.BOOLEAN, 1),
        (DataType.TEXT, None),
    ],
)
def test_fixed_sizes(data_type: DataType, expected: int | None):
    assert codec_for(data_type).fixed_size == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("int", DataType.INTEGER),
        ("INTEGER", DataType.INTEGER),
        ("BigInt", DataType.INTEGER),
        ("varchar", DataType.TEXT),
        ("  bool  ", DataType.BOOLEAN),
        ("REAL", DataType.FLOAT),
    ],
)
def test_sql_type_aliases(name: str, expected: DataType):
    assert DataType.from_sql_name(name) is expected


def test_unknown_type_name_is_rejected():
    with pytest.raises(ValueError, match="unknown data type"):
        DataType.from_sql_name("BLOB")


def test_text_length_prefix_is_bytes_not_characters():
    # A 4-character string of 3-byte code points needs 12 bytes, not 4.
    schema = Schema.of(Column("s", DataType.TEXT))
    raw = encode_record(schema, ("世界世界",))
    assert int.from_bytes(raw[1:5], "little") == 12


# -- schema ----------------------------------------------------------------


def test_schema_needs_at_least_one_column():
    with pytest.raises(SchemaError, match="at least one column"):
        Schema(())


def test_duplicate_column_names_are_rejected_case_insensitively():
    with pytest.raises(SchemaError, match="duplicate"):
        Schema.of(Column("id", DataType.INTEGER), Column("ID", DataType.TEXT))


def test_primary_key_cannot_be_nullable():
    with pytest.raises(SchemaError, match="cannot be nullable"):
        Column("id", DataType.INTEGER, nullable=True, primary_key=True)


def test_composite_primary_keys_are_not_supported_yet():
    with pytest.raises(SchemaError, match=r"[Cc]omposite"):
        Schema.of(
            Column("a", DataType.INTEGER, nullable=False, primary_key=True),
            Column("b", DataType.INTEGER, nullable=False, primary_key=True),
        )


def test_column_lookup_is_case_insensitive(schema: Schema):
    assert schema.index_of("NAME") == 1
    assert schema.column("Id").data_type is DataType.INTEGER
    with pytest.raises(SchemaError, match="no column named"):
        schema.index_of("nope")


def test_fixed_width_detection(schema: Schema):
    assert schema.is_fixed_width is False
    assert schema.fixed_row_size is None

    fixed = Schema.of(
        Column("a", DataType.INTEGER),
        Column("b", DataType.BOOLEAN),
    )
    assert fixed.is_fixed_width is True
    assert fixed.fixed_row_size == 1 + 8 + 1
