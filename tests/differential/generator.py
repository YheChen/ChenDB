"""Random schemas, rows and queries — inside the grammar both engines share.

A generator for a database is not a generator for a compiler. The instinct is to
draw values from a wide domain, and it is exactly wrong: with integers from the
whole 64-bit range no join ever matches, every ``GROUP BY`` makes one group per
row, and a hundred thousand cases exercise one code path. The domains here are
**deliberately tiny** — seven integers, five strings, and NULL — which makes
duplicate keys, multi-row groups, empty groups and unmatched rows the common case
rather than something you wait for. Small domains also put int64 overflow out of
reach by construction rather than by filtering it out afterwards.

The same reasoning shapes the schema. One table with random columns can never
produce an interesting join, so :func:`schema` builds a parent and a child whose
key column is drawn from *three* pools: keys the parent has, keys it has not, and
NULL. That is what makes a single join have matched rows, unmatched rows and
unknown-keyed rows at once — the difference between exercising an outer join and
merely calling one.

Two structural decisions do most of the work.

**Every projection is aliased** ``c0``, ``c1``, … . One line, and it buys three
things: the column *names* match between engines (unaliased they diverge
systematically — ChenDB says ``avg(parent.p_int)``, SQLite says ``AVG(p_int)``),
``ORDER BY c0`` is legal in both, and ChenDB's rule that a sort key must appear in
the select list stops being a restriction to work around.

**The expression builder is typed.** :func:`expression` is asked for a type and
returns only that type. This is not tidiness — it is what keeps generated SQL
inside the *intersection* of the two dialects with no post-hoc filtering. ChenDB
rejects ``s > 1``, ``s + 1`` and ``WHERE i`` where SQLite coerces all three, so an
untyped builder would emit thousands of one-sided errors and drown the signal in
its own noise.

Determinism is the constraint everything bends around, because a query whose
answer is not uniquely defined cannot be compared with anything:

* ``ORDER BY`` over a non-unique key leaves tied rows in an unspecified order.
  Generated freely anyway — that is where a NULL-ordering bug lives — because the
  oracle compares the *sort-key sequence* exactly and the rows as a multiset, both
  of which are defined. See :mod:`tests.differential.oracle`.
* ``LIMIT`` without a total order picks an unspecified *subset*, which no
  comparison can rescue. So :attr:`Query.total_order` gates it, and that flag is
  computed from whether a provably unique non-null projection is among the sort
  keys — not guessed.
* A bare column beside ``GROUP BY`` has no defined value. SQLite invents one;
  ChenDB refuses, correctly. Never generated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Final

from tests.differential.dialect import (
    NULL_ORDER_ASC,
    NULL_ORDER_DESC,
    sqlite_type_name,
)

__all__ = [
    "CHENDB",
    "QUERIES_PER_CASE",
    "SQLITE",
    "Case",
    "ColumnSpec",
    "Query",
    "SchemaSpec",
    "TableSpec",
    "case",
    "features_of",
]

CHENDB: Final = "chendb"
SQLITE: Final = "sqlite"

#: Queries per schema. Setup costs about 1.2 ms and a query about 0.2 ms, so one
#: query per schema would spend most of a run on ``CREATE TABLE``. Sixteen
#: amortises it to the point where the engine, not the fixture, is what is timed.
QUERIES_PER_CASE: Final = 16

# --------------------------------------------------------------------------
# Domains
# --------------------------------------------------------------------------

INTEGER, FLOAT, BOOLEAN, TEXT = "INTEGER", "FLOAT", "BOOLEAN", "TEXT"
_TYPES: Final = (INTEGER, FLOAT, BOOLEAN, TEXT)

#: Small on purpose. ``0`` so dividing by a column can divide by zero, ``1``
#: because the identity hides bugs that scale, and negatives so truncation toward
#: zero is actually exercised rather than assumed.
_INTEGERS: Final = (-3, -1, 0, 1, 2, 3, 7)

#: ``''`` because an empty string is not NULL and the two get confused; ``'A'``
#: and ``'a'`` because case-sensitive collation is a real difference between
#: engines and this is where it would show.
_TEXTS: Final = ("", "a", "A", "b", "ab")

#: ``0.0`` and ``-0.5`` so a sum can cancel to zero and a comparison straddle it.
#: No inf or NaN: ChenDB refuses to store them since Milestone 17, and SQLite
#: turns them into NULL, so generating one would test the INSERT path and nothing
#: else.
_FLOATS: Final = (-0.5, 0.0, 0.5, 1.5, 2.25)

_BOOLEANS: Final = (True, False)

#: How often a nullable column is NULL. A quarter — high, because NULL is where
#: three-valued logic lives and a realistic rate would make it rare.
_NULL_RATE: Final = 0.25

#: Doubled at 0 and 1. An empty table and a one-row table are where off-by-ones
#: and "aggregate over no input" live, and a uniform draw would visit them least.
_ROW_COUNTS: Final = (0, 0, 1, 1, 2, 3, 5, 8)


# --------------------------------------------------------------------------
# The schema
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False

    def ddl(self, dialect: str) -> str:
        parts = [self.name, sqlite_type_name(self.type) if dialect == SQLITE else self.type]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        elif not self.nullable:
            parts.append("NOT NULL")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    rows: tuple[tuple[Any, ...], ...]
    indexed: tuple[str, ...] = ()
    """Columns given a non-unique index, so the planner has a choice to make."""

    @property
    def key(self) -> ColumnSpec:
        return next(column for column in self.columns if column.primary_key)

    def of_type(self, *types: str) -> tuple[ColumnSpec, ...]:
        return tuple(column for column in self.columns if column.type in types)

    def ddl(self, dialect: str) -> list[str]:
        columns = ", ".join(column.ddl(dialect) for column in self.columns)
        statements = [f"CREATE TABLE {self.name} ({columns});"]
        for column in self.indexed:
            statements.append(
                f"CREATE INDEX ix_{self.name}_{column} ON {self.name} ({column});"
            )
        if self.rows:
            values = ",\n  ".join(
                "(" + ", ".join(literal(value) for value in row) + ")" for row in self.rows
            )
            statements.append(f"INSERT INTO {self.name} VALUES\n  {values};")
        return statements


@dataclass(frozen=True, slots=True)
class SchemaSpec:
    tables: tuple[TableSpec, ...]

    def table(self, name: str) -> TableSpec:
        return next(table for table in self.tables if table.name == name)

    def setup(self, dialect: str) -> list[str]:
        """The DDL, indexes and rows, rendered for one engine.

        Rendered from the spec per dialect rather than by rewriting one engine's
        SQL into the other's. Text substitution over a whole script would happily
        corrupt a row whose *value* contains a type name.
        """
        return [line for table in self.tables for line in table.ddl(dialect)]


def literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, float):
        return repr(value)  # always carries a decimal point
    return str(value)


def _draw(source: random.Random, type_name: str) -> Any:
    match type_name:
        case "INTEGER":
            return source.choice(_INTEGERS)
        case "TEXT":
            return source.choice(_TEXTS)
        case "FLOAT":
            return source.choice(_FLOATS)
        case _:
            return source.choice(_BOOLEANS)


def _cell(source: random.Random, column: ColumnSpec) -> Any:
    if column.nullable and source.random() < _NULL_RATE:
        return None
    return _draw(source, column.type)


def _columns(source: random.Random, prefix: str) -> tuple[ColumnSpec, ...]:
    """A primary key, then one column of every type.

    Every type every time rather than a random subset: a schema with no BOOLEAN
    column tests nothing about booleans, and with four types a random subset
    would drop one most of the time.
    """
    return (
        ColumnSpec(f"{prefix}id", INTEGER, nullable=False, primary_key=True),
        *(
            ColumnSpec(f"{prefix}_{name[:3].lower()}", name, nullable=source.random() < 0.7)
            for name in _TYPES
        ),
    )


def schema(source: random.Random) -> SchemaSpec:
    """A parent and a child, with the child's key column deliberately messy.

    The primary key is *numbered*, not drawn. Two reasons: a drawn key from a
    seven-value domain would collide constantly, and a duplicate primary key is
    an error in both engines — so uniqueness has to come from construction or
    every case would diverge for the same uninteresting reason.
    """
    parent_columns = _columns(source, "p")
    parent = TableSpec(
        "parent",
        parent_columns,
        _rows(source, parent_columns, source.choice(_ROW_COUNTS)),
        indexed=_indexed(source, parent_columns),
    )

    child_columns = (
        ColumnSpec("cid", INTEGER, nullable=False, primary_key=True),
        ColumnSpec("parent_id", INTEGER, nullable=True),
        *_columns(source, "c")[1:],
    )
    present = [row[0] for row in parent.rows]
    absent = [value for value in _INTEGERS if value not in present] or [99]

    child_rows: list[tuple[Any, ...]] = []
    for index in range(source.choice(_ROW_COUNTS)):
        draw = source.random()
        if draw < 0.15 or not present:
            parent_id: Any = None  # a NULL join key: matches nothing, not even NULL
        elif draw < 0.75:
            parent_id = source.choice(present)  # a matched row
        else:
            parent_id = source.choice(absent)  # an orphan: what an outer join keeps
        rest = tuple(_cell(source, column) for column in child_columns[2:])
        child_rows.append((index, parent_id, *rest))

    child = TableSpec(
        "child",
        child_columns,
        tuple(child_rows),
        indexed=_indexed(source, child_columns),
    )
    return SchemaSpec((parent, child))


def _indexed(source: random.Random, columns: tuple[ColumnSpec, ...]) -> tuple[str, ...]:
    """An index on one non-key column, a third of the time.

    Not for speed — these tables have at most eight rows. It is so the *planner*
    has two access paths to choose between, which is the only way a generated
    query can catch an index scan and a sequential scan disagreeing. One did.
    """
    candidates = [column.name for column in columns if not column.primary_key]
    if not candidates or source.random() > 0.33:
        return ()
    return (source.choice(candidates),)


def _rows(
    source: random.Random, columns: tuple[ColumnSpec, ...], count: int
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(index if column.primary_key else _cell(source, column) for column in columns)
        for index in range(count)
    )


# --------------------------------------------------------------------------
# Typed expressions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expr:
    sql: str
    type: str
    nullable: bool = True
    unique: bool = False
    """Provably distinct *within its own table* — a primary key."""
    owner: str = ""
    """The alias it came from.

    A primary key is unique in its table, not in a join's output: ``a.pid``
    repeats once per matching ``b`` row. So a total order over a join needs a
    unique key from every source, which has to be tracked per source rather than
    per column. See :func:`_ordering`, which got this wrong first.
    """
    reduced: bool = False
    """Derived by folding many float values, so bit-exact equality is too strict."""


@dataclass(frozen=True, slots=True)
class Source:
    """One table in the ``FROM``, under the name a column reference must use."""

    table: TableSpec
    alias: str

    def column(self, column: ColumnSpec) -> Expr:
        return Expr(
            f"{self.alias}.{column.name}",
            column.type,
            nullable=column.nullable,
            unique=column.primary_key,
            owner=self.alias,
        )


def _columns_of(sources: list[Source], *types: str) -> list[Expr]:
    wanted = types or _TYPES
    return [
        source.column(column)
        for source in sources
        for column in source.table.columns
        if column.type in wanted
    ]


def expression(
    source: random.Random, sources: list[Source], want: str, depth: int = 0
) -> Expr:
    """An expression of exactly type ``want``.

    Typed all the way down, which is what keeps the output inside both dialects.
    An untyped builder emits ``'a' + 1`` and ``s > 1`` constantly; ChenDB refuses
    both — correctly, and like PostgreSQL — so every one would be a one-sided
    error rather than a comparison.
    """
    if want == BOOLEAN:
        return predicate(source, sources, depth)

    columns = _columns_of(sources, want)
    if depth >= 2 or not columns or source.random() < 0.35:
        if columns and source.random() < 0.7:
            return source.choice(columns)
        return Expr(literal(_draw(source, want)), want, nullable=False)

    left = source.choice(columns)
    if want == TEXT:
        return left  # no string operators are implemented in ChenDB

    operator = source.choice(("+", "-", "*", "/", "%"))
    if operator == "%" and want == FLOAT:
        # SQLite's `%` casts both operands to integers first, so `7.5 % 2` is 1.0
        # there and 1.5 here (which is PostgreSQL's answer). A defensible
        # difference in *meaning*, so it is fixed by not generating it — visible
        # in the SQL — rather than by teaching the oracle to look away.
        operator = source.choice(("+", "-", "*"))

    if operator in ("/", "%"):
        # A nonzero literal most of the time. The rest divide by a column and so
        # reach ChenDB's division-by-zero error against SQLite's NULL on purpose,
        # but not in every single case.
        right = (
            source.choice([expr for expr in columns if not expr.unique] or columns)
            if source.random() < 0.2
            else Expr(
                literal(source.choice([v for v in _draw_pool(want) if v])),
                want,
                nullable=False,
            )
        )
    else:
        right = expression(source, sources, want, depth + 1)

    reduced = want == FLOAT and (left.reduced or right.reduced or operator in "+-*")
    return Expr(
        f"({left.sql} {operator} {right.sql})",
        want,
        nullable=left.nullable or right.nullable,
        reduced=reduced,
    )


def _draw_pool(type_name: str) -> tuple[Any, ...]:
    return _INTEGERS if type_name == INTEGER else _FLOATS


def predicate(source: random.Random, sources: list[Source], depth: int = 0) -> Expr:
    """A BOOLEAN expression — so ``WHERE`` is always a condition.

    ChenDB refuses a non-boolean ``WHERE`` since Milestone 17, which is what this
    guarantees by construction. It was a silent wrong answer before that: ``5 is
    True`` is ``False``, so ``WHERE v`` matched no rows and said nothing.
    """
    shape = source.random()
    columns = _columns_of(sources)

    if shape < 0.12 and (booleans := _columns_of(sources, BOOLEAN)):
        # A bare BOOLEAN column really is a condition, in both engines.
        chosen = source.choice(booleans)
        return Expr(chosen.sql, BOOLEAN, nullable=chosen.nullable)

    if shape < 0.24 and columns:
        chosen = source.choice(columns)
        negated = "NOT " if source.random() < 0.5 else ""
        return Expr(f"{chosen.sql} IS {negated}NULL", BOOLEAN, nullable=False)

    if shape < 0.36 and depth < 2:
        left = predicate(source, sources, depth + 1)
        right = predicate(source, sources, depth + 1)
        connective = source.choice(("AND", "OR"))
        return Expr(f"({left.sql} {connective} {right.sql})", BOOLEAN)

    if shape < 0.42 and depth < 2:
        inner = predicate(source, sources, depth + 1)
        return Expr(f"(NOT {inner.sql})", BOOLEAN)

    # A comparison, both sides the same type — the only kind either engine will
    # take without a cast.
    kind = source.choice(_TYPES)
    typed = _columns_of(sources, kind)
    if not typed:
        return Expr("TRUE", BOOLEAN, nullable=False)
    left = source.choice(typed)
    right = (
        source.choice(typed)
        if source.random() < 0.3
        else Expr(literal(_draw(source, kind)), kind, nullable=False)
    )
    operator = (
        source.choice(("=", "<>"))
        if kind == BOOLEAN
        else source.choice(("=", "<>", "<", "<=", ">", ">="))
    )
    return Expr(f"{left.sql} {operator} {right.sql}", BOOLEAN)


def aggregate(source: random.Random, sources: list[Source]) -> Expr:
    """``COUNT(*)``, or a function over a column of a type it accepts.

    ``SUM`` and ``AVG`` take only numbers — ChenDB refuses the rest since
    Milestone 17, having previously returned ``'abd'`` for ``SUM`` over TEXT and
    leaked a raw Python ``TypeError`` out of ``AVG``.
    """
    if source.random() < 0.3:
        return Expr("COUNT(*)", INTEGER, nullable=False)

    function = source.choice(("COUNT", "SUM", "AVG", "MIN", "MAX"))
    types = (INTEGER, FLOAT) if function in ("SUM", "AVG") else _TYPES
    columns = _columns_of(sources, *types)
    if not columns:
        return Expr("COUNT(*)", INTEGER, nullable=False)

    chosen = source.choice(columns)
    match function:
        case "COUNT":
            return Expr(f"COUNT({chosen.sql})", INTEGER, nullable=False)
        case "AVG":
            return Expr(f"AVG({chosen.sql})", FLOAT, reduced=True)
        case "SUM":
            return Expr(f"SUM({chosen.sql})", chosen.type, reduced=chosen.type == FLOAT)
        case _:
            return Expr(f"{function}({chosen.sql})", chosen.type)


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Query:
    """One generated statement, and what the generator knows about its answer.

    ``sort_key_indices`` and ``total_order`` are not decoration: they are the only
    reason a query whose ``ORDER BY`` has ties can be compared at all. The
    generator *knows* what it built — the sort keys were chosen from the aliased
    projections — where the oracle would have to guess.
    """

    sql: str
    sqlite_sql: str
    shape: str
    kind: str = "select"
    """``select``, ``update`` or ``delete``."""
    sort_key_indices: tuple[int, ...] = ()
    """Output positions of the ``ORDER BY`` keys, in order. Empty if unordered."""
    total_order: bool = False
    """Whether the ``ORDER BY`` pins the row sequence uniquely."""
    tolerant_columns: frozenset[int] = frozenset()
    """Outputs folded from many floats, where bit-exact equality is too strict."""
    features: frozenset[str] = frozenset()
    """What corners this query reaches. A coverage floor is asserted over these."""


@dataclass(slots=True)
class _Select:
    """A ``SELECT`` under construction, so the clauses can see each other."""

    source: random.Random
    sources: list[Source]
    shape: str
    projections: list[Expr] = field(default_factory=list)
    group_keys: list[Expr] = field(default_factory=list)
    where: Expr | None = None
    having: Expr | None = None
    order_by: list[tuple[int, bool]] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    joins: list[str] = field(default_factory=list)
    features: set[str] = field(default_factory=set)

    def add(self, expression_: Expr) -> int:
        self.projections.append(expression_)
        return len(self.projections) - 1

    def render(self, dialect: str) -> str:
        select = ", ".join(
            f"{expression_.sql} AS c{index}"
            for index, expression_ in enumerate(self.projections)
        )
        first = self.sources[0]
        parts = [f"SELECT {select}", f"FROM {_named(first)}"]
        parts.extend(self.joins)
        if self.where is not None:
            parts.append(f"WHERE {self.where.sql}")
        if self.group_keys:
            parts.append("GROUP BY " + ", ".join(key.sql for key in self.group_keys))
        if self.having is not None:
            parts.append(f"HAVING {self.having.sql}")
        if self.order_by:
            keys = []
            for index, descending in self.order_by:
                direction = "DESC" if descending else "ASC"
                nulls = NULL_ORDER_DESC if descending else NULL_ORDER_ASC
                keys.append(
                    f"c{index} {direction}" + (f" {nulls}" if dialect == SQLITE else "")
                )
            parts.append("ORDER BY " + ", ".join(keys))
        if self.limit is not None:
            parts.append(f"LIMIT {self.limit}")
            if self.offset:
                parts.append(f"OFFSET {self.offset}")
        return " ".join(parts) + ";"


def _named(entry: Source) -> str:
    return (
        entry.table.name
        if entry.alias == entry.table.name
        else f"{entry.table.name} {entry.alias}"
    )


#: Query shapes and their weights. Weighting alone is a hope, so the corners
#: inside each shape are drawn deliberately as well — a nullable GROUP BY key most
#: of the time, a HALF of HAVINGs that reject every group — and a coverage floor
#: in ``test_harness.py`` fails the build if any named corner stops appearing.
_SHAPES: Final = (
    ("scan", 28),
    ("aggregate", 12),
    ("grouped", 20),
    ("join", 18),
    ("self_join", 8),
    ("dml", 14),
)


def query(source: random.Random, spec: SchemaSpec) -> Query:
    shape = source.choices(
        [name for name, _ in _SHAPES], weights=[weight for _, weight in _SHAPES]
    )[0]
    if shape == "dml":
        return _dml(source, spec)
    return _select(source, spec, shape)


def _select(source: random.Random, spec: SchemaSpec, shape: str) -> Query:
    builder = _Select(source, _sources_for(source, spec, shape), shape)
    if shape in ("join", "self_join"):
        builder.joins.append(_join_clause(source, builder))
    if shape == "grouped":
        _grouped(source, builder)
    elif shape == "aggregate":
        _aggregate_only(source, builder)
    else:
        _plain(source, builder)
    _tail(source, builder)
    _label(builder, spec)

    total, sort_keys = _ordering(builder)
    return Query(
        sql=builder.render(CHENDB),
        sqlite_sql=builder.render(SQLITE),
        shape=shape,
        sort_key_indices=sort_keys,
        total_order=total,
        tolerant_columns=frozenset(
            index
            for index, expression_ in enumerate(builder.projections)
            if expression_.reduced
        ),
        features=frozenset(builder.features),
    )


def _sources_for(source: random.Random, spec: SchemaSpec, shape: str) -> list[Source]:
    if shape == "self_join":
        table = source.choice(spec.tables)
        return [Source(table, "a"), Source(table, "b")]
    if shape == "join":
        return [
            Source(spec.table("parent"), "parent"),
            Source(spec.table("child"), "child"),
        ]
    table = source.choice(spec.tables)
    return [Source(table, table.name)]


def _join_clause(source: random.Random, builder: _Select) -> str:
    """``JOIN b ON …``, equijoin most of the time.

    An equality is what lets the planner pick a hash join, so it has to be the
    common case or the hash-join path goes untested. The rest are comparisons,
    which only a nested loop can evaluate.
    """
    right = builder.sources[1]
    left = builder.sources[0]
    if builder.shape == "self_join":
        condition = f"a.{left.table.key.name} <> b.{right.table.key.name}"
        builder.features.add("self_join")
    elif source.random() < 0.8:
        condition = f"parent.{left.table.key.name} = child.parent_id"
        builder.features.add("null_join_key")
        builder.features.add("unmatched_join_row")
    else:
        condition = f"parent.{left.table.key.name} < child.parent_id"
    return f"JOIN {_named(right)} ON {condition}"


def _plain(source: random.Random, builder: _Select) -> None:
    for _ in range(source.randint(1, 3)):
        builder.add(expression(source, builder.sources, source.choice(_TYPES)))
    if source.random() < 0.65:
        builder.where = predicate(source, builder.sources)
        builder.features.add("where")


def _aggregate_only(source: random.Random, builder: _Select) -> None:
    """Aggregates with no ``GROUP BY``: one group, which exists even over no rows.

    ``SELECT COUNT(*) FROM empty`` is ``0`` and not nothing, and ``SUM`` over the
    same is NULL. Getting that pair right is the point of this shape.
    """
    for _ in range(source.randint(1, 3)):
        builder.add(aggregate(source, builder.sources))
    if source.random() < 0.5:
        builder.where = predicate(source, builder.sources)
        builder.features.add("aggregate_over_empty_input")


def _grouped(source: random.Random, builder: _Select) -> None:
    columns = _columns_of(builder.sources)
    nullable = [column for column in columns if column.nullable]
    # Nullable most of the time, so a NULL group key is the norm rather than a
    # rarity. NULL is its own group and two NULLs group together, which is the
    # one place `GROUP BY` disagrees with `=`.
    pool = nullable if nullable and source.random() < 0.6 else columns
    key = source.choice(pool)
    builder.group_keys.append(key)
    if key.nullable:
        builder.features.add("null_group_key")

    builder.add(key)
    for _ in range(source.randint(1, 2)):
        builder.add(aggregate(source, builder.sources))

    if source.random() < 0.5:
        builder.where = predicate(source, builder.sources)
    if source.random() < 0.5:
        builder.features.add("having")
        if source.random() < 0.5:
            # Rejects every group, so an empty result is a common outcome.
            builder.having = Expr("COUNT(*) < 0", BOOLEAN, nullable=False)
            builder.features.add("empty_group")
        else:
            builder.having = Expr("COUNT(*) >= 1", BOOLEAN, nullable=False)


def _tail(source: random.Random, builder: _Select) -> None:
    if not builder.projections or source.random() > 0.7:
        return
    count = source.randint(1, min(2, len(builder.projections)))
    for index in source.sample(range(len(builder.projections)), count):
        descending = source.random() < 0.5
        builder.order_by.append((index, descending))
        if descending:
            builder.features.add("order_by_desc")
    builder.features.add("order_by")

    total, _ = _ordering(builder)
    if total and source.random() < 0.4:
        builder.limit = source.randint(0, 4)
        builder.features.add("limit")
        if source.random() < 0.4:
            builder.offset = source.randint(1, 3)
            builder.features.add("offset")


def _ordering(builder: _Select) -> tuple[bool, tuple[int, ...]]:
    """Whether the ``ORDER BY`` is total, and which outputs are its keys.

    Getting this wrong is a false accusation, and it was one: the first version
    asked only whether *some* sort key was a unique column, so a self-join ordered
    by ``b.pid`` counted as total. It is not — the join emits one row per matching
    ``a``, so every ``b.pid`` repeats — and the tester duly reported the two
    engines' equally legal tie orders as a divergence. A total order over a join
    needs a unique non-null key **from every source in the FROM**.

    After a ``GROUP BY`` the groups are distinct by definition, so ordering by all
    of the group keys is total whatever the sources were.

    Only when this is true is ``LIMIT``'s *subset* defined, which is why it gates
    it.
    """
    if not builder.order_by:
        return False, ()
    indices = tuple(index for index, _ in builder.order_by)

    if builder.group_keys:
        keyed = {key.sql for key in builder.group_keys}
        ordered = {builder.projections[index].sql for index in indices}
        return keyed <= ordered, indices

    covered = {
        builder.projections[index].owner
        for index in indices
        if builder.projections[index].unique and not builder.projections[index].nullable
    }
    return {entry.alias for entry in builder.sources} <= covered, indices


def _label(builder: _Select, spec: SchemaSpec) -> None:
    for entry in builder.sources:
        if not entry.table.rows:
            builder.features.add("zero_row_table")
        if entry.table.indexed:
            builder.features.add("index_present")
        if any(
            not column.nullable and not column.primary_key for column in entry.table.columns
        ):
            builder.features.add("not_null_column")
    if builder.where is not None and builder.where.type == BOOLEAN:
        builder.features.add("boolean_predicate")


def _dml(source: random.Random, spec: SchemaSpec) -> Query:
    """An ``UPDATE`` or a ``DELETE``, compared on the rows it leaves behind.

    Run inside a transaction that is rolled back, so one schema serves many DML
    queries and none can affect the next.
    """
    table = source.choice(spec.tables)
    sources = [Source(table, table.name)]
    condition = predicate(source, sources) if source.random() < 0.8 else None
    suffix = f" WHERE {condition.sql}" if condition is not None else ""

    if source.random() < 0.5:
        writable = [
            column
            for column in table.columns
            if not column.primary_key and column.name != "parent_id"
        ]
        column = source.choice(writable)
        value = expression(source, sources, column.type)
        if column.type == BOOLEAN:
            value = predicate(source, sources)
        # A NOT NULL column may not be assigned a NULL literal, and ChenDB
        # refuses that at bind time.
        sql = f"UPDATE {table.name} SET {column.name} = {value.sql}{suffix};"
        kind = "update"
        feature = "dml_update"
    else:
        sql = f"DELETE FROM {table.name}{suffix};"
        kind = "delete"
        feature = "dml_delete"

    return Query(
        sql=sql,
        sqlite_sql=sql,
        shape="dml",
        kind=kind,
        features=frozenset({feature}),
    )


# --------------------------------------------------------------------------
# A case
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One schema and many queries. The seed is its name."""

    seed: int
    schema: SchemaSpec
    queries: tuple[Query, ...]


def case(seed: int, *, queries: int = QUERIES_PER_CASE) -> Case:
    """Build case ``seed``. Deterministic: same seed, same bytes, forever.

    One ``Random`` per case, seeded here, and nothing in this module reads the
    clock, the environment or an unordered container. That is what makes a
    failure reproducible from a single integer in a CI log.
    """
    source = random.Random(seed)
    spec = schema(source)
    return Case(seed, spec, tuple(query(source, spec) for _ in range(queries)))


def features_of(instance: Case) -> frozenset[str]:
    return (
        frozenset().union(*(item.features for item in instance.queries))
        if instance.queries
        else frozenset()
    )
