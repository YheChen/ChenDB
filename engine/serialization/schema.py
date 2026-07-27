"""Column and table schema definitions.

A :class:`Schema` is an immutable, ordered list of columns.  It is the contract
between a row of Python values and its byte representation: encoding and
decoding are meaningless without it, which is exactly why a database needs a
persistent catalog.

Schemas themselves are not persisted here: Milestone 4's catalog stores them as
rows of ``chendb_columns`` and rebuilds a :class:`Schema` on lookup.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from engine.errors import SchemaError
from engine.serialization.types import DataType, codec_for

__all__ = ["Column", "Schema"]

#: Identifiers are compared case-insensitively but stored as written, matching
#: the behaviour most people expect from SQLite. (PostgreSQL folds unquoted
#: identifiers to lower case instead.)
_MAX_IDENTIFIER_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a table."""

    name: str
    data_type: DataType
    nullable: bool = True
    primary_key: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise SchemaError("column name must not be empty")
        if len(self.name) > _MAX_IDENTIFIER_LENGTH:
            raise SchemaError(
                f"column name {self.name!r} exceeds {_MAX_IDENTIFIER_LENGTH} characters"
            )
        if self.primary_key and self.nullable:
            raise SchemaError(
                f"column {self.name!r} is a primary key and therefore cannot be nullable"
            )

    @property
    def fixed_size(self) -> int | None:
        return codec_for(self.data_type).fixed_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.data_type.sql_name,
            "nullable": self.nullable,
            "primary_key": self.primary_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Column:
        return cls(
            name=data["name"],
            data_type=DataType.from_sql_name(data["type"]),
            nullable=bool(data.get("nullable", True)),
            primary_key=bool(data.get("primary_key", False)),
        )


@dataclass(frozen=True, slots=True)
class Schema:
    """An ordered, immutable collection of columns."""

    columns: tuple[Column, ...]

    def __post_init__(self) -> None:
        if not self.columns:
            raise SchemaError("a schema needs at least one column")
        seen: set[str] = set()
        for column in self.columns:
            key = column.name.casefold()
            if key in seen:
                raise SchemaError(f"duplicate column name {column.name!r}")
            seen.add(key)
        if sum(1 for column in self.columns if column.primary_key) > 1:
            raise SchemaError("composite primary keys are not supported yet (Milestone 5)")

    @classmethod
    def of(cls, *columns: Column) -> Schema:
        """Convenience constructor: ``Schema.of(Column(...), Column(...))``."""
        return cls(tuple(columns))

    # -- lookup ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.columns)

    def __iter__(self) -> Iterator[Column]:
        return iter(self.columns)

    def __getitem__(self, index: int) -> Column:
        return self.columns[index]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def index_of(self, name: str) -> int:
        """Position of ``name``, matched case-insensitively."""
        key = name.casefold()
        for index, column in enumerate(self.columns):
            if column.name.casefold() == key:
                return index
        raise SchemaError(f"no column named {name!r}; have {', '.join(self.column_names)}")

    def column(self, name: str) -> Column:
        return self.columns[self.index_of(name)]

    @property
    def primary_key(self) -> Column | None:
        for column in self.columns:
            if column.primary_key:
                return column
        return None

    # -- layout ------------------------------------------------------------

    @property
    def null_bitmap_size(self) -> int:
        """Bytes of null bitmap: one bit per column, rounded up."""
        return (len(self.columns) + 7) // 8

    @property
    def is_fixed_width(self) -> bool:
        """True when every column has a fixed encoded size.

        Such a schema has a constant row width, which makes it possible to
        compute a column's offset arithmetically instead of walking the row.
        """
        return all(column.fixed_size is not None for column in self.columns)

    @property
    def fixed_row_size(self) -> int | None:
        """Encoded row size, or ``None`` if any column is variable-width."""
        if not self.is_fixed_width:
            return None
        return self.null_bitmap_size + sum(
            column.fixed_size or 0 for column in self.columns
        )

    def row_to_dict(self, values: Sequence[Any]) -> dict[str, Any]:
        """Zip a positional row against the column names, for display."""
        if len(values) != len(self.columns):
            raise SchemaError(
                f"row has {len(values)} values but the schema has {len(self.columns)} columns"
            )
        return dict(zip(self.column_names, values, strict=True))

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"columns": [column.to_dict() for column in self.columns]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Schema:
        return cls(tuple(Column.from_dict(item) for item in data["columns"]))

    def __repr__(self) -> str:
        rendered = ", ".join(
            f"{column.name} {column.data_type.sql_name}"
            f"{'' if column.nullable else ' NOT NULL'}"
            f"{' PK' if column.primary_key else ''}"
            for column in self.columns
        )
        return f"<Schema {rendered}>"
