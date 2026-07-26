"""The cost model: deciding which plan is cheaper before running either.

Milestone 5 built two access paths and chose between them by rule — "use an
index whenever one covers a comparison" — which is wrong above about 14%
selectivity, measurably and on purpose.  This module is what makes the choice
by arithmetic instead.

    predicate ──selectivity──▶ estimated rows ──cost──▶ a number
                    ▲                                      │
              statistics                            compare candidates

Two halves, and they fail differently.  **Selectivity** estimation is where the
error usually comes from — it depends on the data and on assumptions about it.
**Costing** is nearly mechanical once the row count is known.  A planner that
picks a terrible plan has almost always mis-estimated the rows, not mis-added
the costs; PostgreSQL's ``EXPLAIN ANALYZE`` puts estimated and actual side by
side for exactly this reason, and so does ChenDB's plan view.

Calibration
-----------
The constants below are **measured for this engine**, not copied from
PostgreSQL, and the difference is the interesting part.

PostgreSQL's defaults say a page read costs 1.0 and processing a tuple costs
0.01 — CPU is a hundred times cheaper than I/O.  That is right for compiled
code against a spinning disk.  It is badly wrong here, for two reasons:

* every "page read" hits the OS page cache and costs a ``pread`` of 4 KiB plus
  a **CRC32 over the whole page** — real work, but only microseconds;
* every row costs an interpreted Python ``decode_record`` plus predicate
  evaluation — which turns out to be the *dominant* term.

Measured on ``benchmarks/index_vs_scan.py`` (20,000 rows, 4 KiB pages), a
sequential scan spends ~3 µs per row and ~2 µs per page.  So ``CPU_TUPLE_COST``
is not 1/100th of a page read here; it is about 1/7th.  Copying PostgreSQL's
ratio would have made the model refuse the index at 0.3% selectivity, where it
in fact still wins by 80x.

Setting ``PAGE_COST = 1.0`` as the unit and fitting the rest to the benchmark
gives the numbers below, and the fit is good across three orders of magnitude:

=========================  =========  ===========  =============
Plan                       Est. cost  Measured     µs per unit
=========================  =========  ===========  =============
index scan, 20 rows               25      0.7 ms          26.7
index scan, 1 000 rows         1 168     22.2 ms          19.0
index scan, 4 000 rows         4 667     87.2 ms          18.7
index scan, 14 000 rows       16 328    307.5 ms          18.8
sequential scan + filter       4 303     81.3 ms          18.9
=========================  =========  ===========  =============

Roughly 18.9 µs per cost unit, near-constant from 25 to 16,328 — a 650x range —
and the same for both access paths, which is the part that matters: a model
that is self-consistent but mis-weights one path against the other picks the
wrong plan while looking well calibrated.

``tests/unit/test_cost_model.py`` pins the crossover down so a change to the
constants cannot silently move it, and ``benchmarks/index_vs_scan.py`` prints
the table above so a change to the *engine* shows up as a drifting µs/unit.

The constants are per-engine and will change. Milestone 7's buffer pool makes a
cached page genuinely free and an uncached one genuinely expensive, at which
point ``RANDOM_PAGE_COST`` starts to mean something and will have to be
re-measured. That is normal: PostgreSQL ships a ``random_page_cost`` knob
precisely because nobody can know it in advance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from engine.executor.binder import BoundColumnRef
from engine.parser.ast import (
    BinaryOp,
    BinaryOperator,
    Expression,
    IsNullTest,
    Literal,
    UnaryOp,
    UnaryOperator,
)
from engine.planner.statistics import ColumnStatistics, TableStatistics
from engine.serialization.types import DataType

__all__ = [
    "CPU_INDEX_COST",
    "CPU_PREDICATE_COST",
    "CPU_TUPLE_COST",
    "DEFAULT_EQ_SELECTIVITY",
    "DEFAULT_INEQ_SELECTIVITY",
    "PAGE_HIT_COST",
    "PAGE_MISS_COST",
    "Cost",
    "distinct_pages_touched",
    "estimate_selectivity",
    "index_scan_cost",
    "seq_scan_cost",
]

#: A page read the buffer pool could not serve: a ``pread``, a CRC32 and the
#: O(1) header check. Measured at 1822 ns; the unit everything else is in.
PAGE_MISS_COST: Final = 1.0

#: A page read served from a frame: two 4 KiB copies and a ``Page`` object,
#: with no syscall and no verification. Measured at 661 ns.
#:
#: Milestone 6 had ``RANDOM_PAGE_COST`` here, on the assumption that Milestone 7
#: would make locality matter. It did not. With the pool the axis that decides
#: cost is **hit against miss**, not sequential against random — there is no
#: seek on an SSD, and the OS page cache was already flattening the difference.
#: Estimating hits is what replaced it; see :func:`distinct_pages_touched`.
PAGE_HIT_COST: Final = 0.36

#: Decoding one record into Python values. Measured at 778 ns — *more than a
#: third of a page miss*, which is the fact that makes this engine's cost model
#: look nothing like PostgreSQL's, where a tuple is 1/100th of a page read.
CPU_TUPLE_COST: Final = 0.43

#: Evaluating one predicate against an already-decoded row. About a third of a
#: decode: walking a small expression tree is real work, but nothing next to
#: unpacking every column. Charging a full ``CPU_TUPLE_COST`` here double-counts
#: and over-costs a filtered sequential scan, which biases every crossover
#: toward the index.
CPU_PREDICATE_COST: Final = 0.14

#: Comparing one key inside a B+ tree node. Cheap: the keys are encoded so the
#: comparison is a ``memcmp``. See :mod:`engine.index.key`.
CPU_INDEX_COST: Final = 0.005

#: Used when there is nothing better. PostgreSQL's equivalents are
#: ``DEFAULT_EQ_SEL`` (0.005) and ``DEFAULT_INEQ_SEL`` (0.3333); the same values
#: are used here because the reasoning behind them — an equality is usually
#: selective, an inequality usually is not — is not engine-specific.
DEFAULT_EQ_SELECTIVITY: Final = 0.005
DEFAULT_INEQ_SELECTIVITY: Final = 1.0 / 3.0

#: Never estimate zero rows. A plan costed at zero looks free and wins every
#: comparison, so one bad estimate would poison every choice after it.
MIN_SELECTIVITY: Final = 1e-6


@dataclass(frozen=True, slots=True)
class Cost:
    """An estimate, split so the plan view can show where it came from.

    ``io`` and ``cpu`` are kept apart because they answer different questions —
    "would a buffer pool help?" against "would a faster predicate help?" — and
    a single total hides both.
    """

    io: float = 0.0
    cpu: float = 0.0
    rows: float = 0.0
    """Estimated rows *out*. Carried with the cost because a parent's cost is a
    function of its child's row count, so the two always travel together."""

    @property
    def total(self) -> float:
        return self.io + self.cpu

    def __add__(self, other: Cost) -> Cost:
        return Cost(io=self.io + other.io, cpu=self.cpu + other.cpu, rows=other.rows)

    def with_rows(self, rows: float) -> Cost:
        return Cost(io=self.io, cpu=self.cpu, rows=rows)

    def __str__(self) -> str:
        return f"cost={self.total:.1f} (io {self.io:.1f} + cpu {self.cpu:.1f}) rows={self.rows:.0f}"


# --------------------------------------------------------------------------
# Costing
# --------------------------------------------------------------------------


def seq_scan_cost(stats: TableStatistics) -> Cost:
    """Read every page once, decode every row.

    Every read is charged as a **miss**: a scan touches each page exactly once,
    so the pool can never serve one — and worse, it evicts everything that was
    in there. That is sequential flooding, and it is why PostgreSQL confines
    large scans to a small ring buffer. ChenDB does not, so a scan is honestly
    costed as all-misses and honestly *behaves* that way.

    The predicate is not costed here: a ``Filter`` above the scan charges its
    own evaluations, and counting them twice would bias every comparison.
    """
    return Cost(
        io=stats.page_count * PAGE_MISS_COST,
        cpu=stats.row_count * CPU_TUPLE_COST,
        rows=float(stats.row_count),
    )


def distinct_pages_touched(fetches: float, page_count: int) -> float:
    """How many *distinct* pages ``fetches`` random row lookups will land on.

    The classic occupancy result: with rows spread evenly over *P* pages, the
    expected number of distinct pages hit by *N* independent fetches is
    ``P * (1 - (1 - 1/P)**N)``. Cárdenas published it in 1975 and every
    optimiser since has needed some version of it — PostgreSQL uses the
    Mackert-Lohman refinement, which also accounts for the cache being finite.

    It is what turns "the index will fetch 14,000 rows" into "…from about 233
    distinct pages, so 233 misses and 13,767 hits", and without it the pool is
    invisible to the planner: every fetch would be charged as a miss and the
    index would look three times more expensive than it is.

    The optimism here is deliberate and worth naming: it assumes a page touched
    once stays resident. With a pool smaller than the working set that is false,
    and the estimate is then too low. Modelling that properly needs the pool
    size *and* the access pattern, which is what ``effective_cache_size`` tries
    to approximate in PostgreSQL.
    """
    if page_count <= 0 or fetches <= 0:
        return 0.0
    distinct = page_count * (1.0 - (1.0 - 1.0 / page_count) ** fetches)
    return min(distinct, fetches, float(page_count))


def index_scan_cost(
    stats: TableStatistics, *, matching_rows: float, height: int, entries_per_leaf: float
) -> Cost:
    """Descend, walk the matching leaves, then fetch one heap page per row.

    That last term is what decides everything. The descent is three page reads
    whatever the query; the fetches are one page read *per matching row*, and
    the same page over and over when several matches share it. With no buffer
    pool there is no deduplication, which is why the crossover sits as low as it
    does — and why Milestone 7 will move it.
    """
    matching = max(matching_rows, 0.0)
    leaves = matching / entries_per_leaf if entries_per_leaf > 0 else 0.0

    # The descent and the leaves are read once each, so they are misses. The
    # heap fetches are where the pool earns its memory: many of them land on a
    # page a previous fetch already brought in.
    misses = distinct_pages_touched(matching, stats.page_count)
    hits = matching - misses

    return Cost(
        io=(height * PAGE_MISS_COST)
        + (leaves * PAGE_MISS_COST)
        + (misses * PAGE_MISS_COST)
        + (hits * PAGE_HIT_COST),
        cpu=(matching * CPU_INDEX_COST) + (matching * CPU_TUPLE_COST),
        rows=matching,
    )


def filter_cost(input_rows: float, *, selectivity: float) -> Cost:
    """Evaluate a predicate once per input row. No I/O of its own."""
    return Cost(
        io=0.0, cpu=input_rows * CPU_PREDICATE_COST, rows=input_rows * selectivity
    )


def project_cost(input_rows: float, *, expressions: int) -> Cost:
    """Evaluate the select list once per row that survives."""
    return Cost(
        io=0.0,
        cpu=input_rows * expressions * CPU_INDEX_COST,
        rows=input_rows,
    )


# --------------------------------------------------------------------------
# Selectivity
# --------------------------------------------------------------------------


def estimate_selectivity(
    predicate: Expression | None, stats: TableStatistics
) -> float:
    """The fraction of rows ``predicate`` is expected to admit, in ``[0, 1]``.

    Everything the planner gets wrong, it gets wrong here.
    """
    if predicate is None:
        return 1.0
    return _clamp(_selectivity(predicate, stats))


def _selectivity(predicate: Expression, stats: TableStatistics) -> float:
    match predicate:
        case BinaryOp(operator=BinaryOperator.AND, left=left, right=right):
            # The independence assumption, and the most consequential wrong
            # assumption in any planner: `city = 'Paris' AND country = 'France'`
            # is estimated as the product of two small numbers, when in reality
            # the second is implied by the first. PostgreSQL added extended
            # statistics (CREATE STATISTICS) in version 10 to let a DBA say
            # otherwise; nothing here can.
            return _selectivity(left, stats) * _selectivity(right, stats)

        case BinaryOp(operator=BinaryOperator.OR, left=left, right=right):
            # Inclusion-exclusion, also assuming independence.
            first, second = _selectivity(left, stats), _selectivity(right, stats)
            return first + second - first * second

        case UnaryOp(operator=UnaryOperator.NOT, operand=operand):
            return 1.0 - _selectivity(operand, stats)

        case IsNullTest(operand=BoundColumnRef() as column, negated=negated):
            info = stats.column(column.column_index)
            if info is None:
                return DEFAULT_EQ_SELECTIVITY
            fraction = info.null_fraction(stats.row_count)
            return 1.0 - fraction if negated else fraction

        case BinaryOp() if predicate.operator.is_comparison:
            return _comparison_selectivity(predicate, stats)

    return DEFAULT_INEQ_SELECTIVITY


def _comparison_selectivity(predicate: BinaryOp, stats: TableStatistics) -> float:
    matched = _as_column_literal(predicate)
    if matched is None:
        # `a < b` between two columns. PostgreSQL uses a fixed guess here too.
        return DEFAULT_INEQ_SELECTIVITY
    column, operator, value = matched

    info = stats.column(column.column_index)
    if info is None:
        return DEFAULT_EQ_SELECTIVITY

    if value is None:
        # `x = NULL` is UNKNOWN for every row in three-valued logic, so it
        # admits none. Not the same as `x IS NULL`, which is why the AST keeps
        # them as different nodes.
        return MIN_SELECTIVITY

    non_null = 1.0 - info.null_fraction(stats.row_count)

    match operator:
        case BinaryOperator.EQ:
            return non_null / info.distinct_count
        case BinaryOperator.NEQ:
            return non_null * (1.0 - 1.0 / info.distinct_count)
        case BinaryOperator.LT | BinaryOperator.LTE:
            return non_null * _range_fraction(info, value, below=True)
        case BinaryOperator.GT | BinaryOperator.GTE:
            return non_null * _range_fraction(info, value, below=False)

    return DEFAULT_INEQ_SELECTIVITY  # pragma: no cover - all comparisons covered


def _range_fraction(info: ColumnStatistics, value: Any, *, below: bool) -> float:
    """Where ``value`` falls between min and max, assuming a uniform spread.

    Uniformity is the assumption a histogram exists to remove: on a column whose
    values cluster at one end, this is wrong by however skewed the data is. It
    is still far better than a fixed guess, because it gets the *shape* right —
    a bound outside the observed range estimates ~0 or ~1, which is correct and
    is the case a fixed guess handles worst.
    """
    if not info.has_range or not isinstance(value, (int, float)):
        return DEFAULT_INEQ_SELECTIVITY

    low, high = float(info.minimum), float(info.maximum)
    point = float(value)
    if high <= low:
        # Every row shares one value: the predicate is all or nothing.
        if below:
            return 1.0 if point > low else MIN_SELECTIVITY
        return 1.0 if point < low else MIN_SELECTIVITY

    fraction = (point - low) / (high - low)
    fraction = min(max(fraction, 0.0), 1.0)
    return fraction if below else 1.0 - fraction


def _as_column_literal(
    predicate: BinaryOp,
) -> tuple[BoundColumnRef, BinaryOperator, Any] | None:
    """Match ``column <op> literal``, mirroring the operator if reversed."""
    left, right = predicate.left, predicate.right
    if isinstance(left, BoundColumnRef) and isinstance(right, Literal):
        return left, predicate.operator, right.value
    if isinstance(right, BoundColumnRef) and isinstance(left, Literal):
        return right, _MIRRORED[predicate.operator], left.value
    return None


_MIRRORED: Final[dict[BinaryOperator, BinaryOperator]] = {
    BinaryOperator.EQ: BinaryOperator.EQ,
    BinaryOperator.NEQ: BinaryOperator.NEQ,
    BinaryOperator.LT: BinaryOperator.GT,
    BinaryOperator.LTE: BinaryOperator.GTE,
    BinaryOperator.GT: BinaryOperator.LT,
    BinaryOperator.GTE: BinaryOperator.LTE,
}


def _clamp(selectivity: float) -> float:
    return min(max(selectivity, MIN_SELECTIVITY), 1.0)


def describe_type(data_type: DataType | None) -> str:
    return data_type.sql_name if data_type is not None else "?"
