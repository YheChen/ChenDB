"""Binary serialization: Python values <-> bytes, driven by a schema.

``types.py`` owns the per-type codecs, ``schema.py`` the column definitions,
and ``record.py`` the row layout (null bitmap followed by values).  Nothing
here knows about pages: a record is just bytes, and where those bytes live is
the storage layer's problem.
"""

from engine.serialization.record import (
    FieldLayout,
    RecordLayout,
    Row,
    decode_record,
    describe_record,
    encode_record,
    estimate_record_size,
)
from engine.serialization.schema import Column, Schema
from engine.serialization.types import Codec, DataType, codec_for

__all__ = [
    "Codec",
    "Column",
    "DataType",
    "FieldLayout",
    "RecordLayout",
    "Row",
    "Schema",
    "codec_for",
    "decode_record",
    "describe_record",
    "encode_record",
    "estimate_record_size",
]
