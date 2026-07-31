"""The cost model: deciding which plan is cheaper before running either.

Milestone 5 built two access paths and chose between them by rule ("use an
index whenever one covers a comparison") which is wrong above about 14%
selectivity, measurably and on purpose.  This module is what makes the choice
by arithmetic instead.

    predicate ──selectivity──▶ estimated rows ──cost──▶ a number
                    ▲                                      │
              statistics                            compare candidates

Two halves, and they fail differently.  **Selectivity** estimation is where the
error usually comes from. It depends on the data and on assumptions about it.
**Costing** is nearly mechanical once the row count is known.  A planner that
picks a terrible plan has almost always mis-estimated the rows, not mis-added
the costs; PostgreSQL's ``EXPLAIN ANALYZE`` puts estimated and actual side by
side for exactly this reason, and so does ChenDB's plan view.

Calibration
-----------
The constants below are **measured for this engine**, not copied from
PostgreSQL, and the difference is the interesting part.

PostgreSQL's defaults say a page read costs 1.0 and processing a tuple costs
0.01, CPU is a hundred times cheaper than I/O.  That is right for compiled
code against a spinning disk.  It is badly wrong here, for two reasons:

* every "page read" hits the OS page cache and costs a ``pread`` of 4 KiB plus
  a **CRC32 over the whole page**, real work, but only microseconds;
* every row costs an interpreted Python ``decode_record`` plus predicate
  evaluation, which turns out to be the *dominant* term.

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

Roughly 18.9 µs per cost unit, near-constant from 25 to 16,328 (a 650x range)
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

import math
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
    "mcv_join_selectivity",
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
#: cost is **hit against miss**, not sequential against random, there is no
#: seek on an SSD, and the OS page cache was already flattening the difference.
#: Estimating hits is what replaced it; see :func:`distinct_pages_touched`.
PAGE_HIT_COST: Final = 0.36

#: Decoding one record into Python values. Measured at 778 ns, *more than a
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
#: are used here because the reasoning behind them, an equality is usually
#: selective, an inequality usually is not, is not engine-specific.
DEFAULT_EQ_SELECTIVITY: Final = 0.005
DEFAULT_INEQ_SELECTIVITY: Final = 1.0 / 3.0

#: Never estimate zero rows. A plan costed at zero looks free and wins every
#: comparison, so one bad estimate would poison every choice after it.
MIN_SELECTIVITY: Final = 1e-6


@dataclass(frozen=True, slots=True)
class Cost:
    """An estimate, split so the plan view can show where it came from.

    ``io`` and ``cpu`` are kept apart because they answer different questions (
    "would a buffer pool help?" against "would a faster predicate help?") and
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
    so the pool can never serve one, and worse, it evicts everything that was
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
    optimiser since has needed some version of it, PostgreSQL uses the
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
    does, and why Milestone 7 will move it.
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
    return Cost(io=0.0, cpu=input_rows * CPU_PREDICATE_COST, rows=input_rows * selectivity)


def project_cost(input_rows: float, *, expressions: int) -> Cost:
    """Evaluate the select list once per row that survives."""
    return Cost(
        io=0.0,
        cpu=input_rows * expressions * CPU_INDEX_COST,
        rows=input_rows,
    )


#: Inserting one row into the hash table: hash the key, index a dict, append to
#: a bucket, and hold the row. Measured at 67 ns.
CPU_HASH_BUILD_COST: Final = 0.037

#: Looking one key up: hash and index, with nothing retained. Measured at 45 ns,
#: **two thirds of a build**, and that asymmetry is not a detail. It is the
#: only thing in the cost model that says which side of a hash join should be
#: the build side, and getting that backwards is the difference between a hash
#: table of ten rows and one of ten million.
CPU_HASH_PROBE_COST: Final = 0.025

#: Comparing two rows during a sort. Measured at 26 ns: a tuple comparison in C,
#: not a walk of an expression tree, so it is a fifth of a predicate evaluation.
#: That is why an `n log n` sort of 5,000 rows costs less than scanning them.
CPU_COMPARE_COST: Final = 0.014

#: Folding one value into one accumulator: a counter, an add, or a compare.
#: Measured at 50 ns.
CPU_AGGREGATE_COST: Final = 0.027

#: What an equijoin between two unknown columns is assumed to select, when
#: neither side has distinct-value statistics. PostgreSQL falls back to
#: ``DEFAULT_EQ_SEL`` here too; the difference is that a join's fallback is
#: applied to the *product* of two row counts, so it is the single estimate most
#: able to be catastrophically wrong.
DEFAULT_JOIN_SELECTIVITY: Final = DEFAULT_EQ_SELECTIVITY


def nested_loop_join_cost(outer_rows: float, inner_rows: float, *, matches: float) -> Cost:
    """Compare every outer row against every inner row.

    ``O(n·m)`` comparisons and no memory. It wins on exactly one shape (a tiny
    outer side) and loses spectacularly on everything else, which is precisely
    what makes it worth keeping as a candidate: the cost model has to *say* so,
    and a planner with one join algorithm has nothing to be right about.

    The inner side is drained into memory once, so what is charged here is
    comparisons and not re-scans. Re-reading the inner child per outer row (
    the textbook naive form) would make this ``O(n·m)`` *scans* rather than
    ``O(n·m)`` comparisons, and the difference is several orders of magnitude.
    So this cost is the *optimistic* one, and the algorithm still loses.
    """
    comparisons = outer_rows * inner_rows
    return Cost(io=0.0, cpu=comparisons * CPU_PREDICATE_COST, rows=matches)


def hash_join_cost(build_rows: float, probe_rows: float, *, matches: float) -> Cost:
    """Build a hash table on one side, probe it with the other.

    ``O(n + m)`` instead of ``O(n·m)``, paid for in memory proportional to the
    build side, so the planner builds on the *smaller* one. It arrives at that
    by arithmetic rather than by a rule: a build entry costs half again what a
    probe does, so putting the bigger side on the build is simply more
    expensive. That is the whole reason the model needs a row-count estimate for
    each side and not just for the result.

    Only equijoins can use it. A join on ``a.x < b.y`` has no key to hash and
    falls back to nested loops, which is why range joins are slow in every
    engine and not just this one.
    """
    return Cost(
        io=0.0,
        cpu=build_rows * CPU_HASH_BUILD_COST + probe_rows * CPU_HASH_PROBE_COST,
        rows=matches,
    )


def sort_cost(input_rows: float, *, keys: int = 1) -> Cost:
    """``n log n`` comparisons, in memory.

    Charged even when the input is one row, because the operator still has to
    *drain its child completely* before it can emit anything. A sort is the
    first thing in this engine that is not a pipeline, and the plan view shows
    it as the place where "time to first row" stops being small.

    No spill to disk: a sort that does not fit is a sort that fails. PostgreSQL
    switches to an external merge at ``work_mem``; ChenDB's ceiling is the row
    limit, which is checked before the sort rather than during it.
    """
    comparisons = input_rows * max(math.log2(max(input_rows, 2.0)), 1.0)
    return Cost(io=0.0, cpu=comparisons * keys * CPU_COMPARE_COST, rows=input_rows)


def aggregate_cost(input_rows: float, *, groups: float, aggregates: int) -> Cost:
    """Hash each row to its group, then fold it into that group's accumulators.

    Linear in rows and independent of the number of groups, which is the
    property that makes hashing the right choice here: sorting first would cost
    ``n log n`` and buy an ordering nobody asked for.
    """
    return Cost(
        io=0.0,
        cpu=input_rows * (CPU_HASH_BUILD_COST + aggregates * CPU_AGGREGATE_COST),
        rows=max(groups, 1.0),
    )


def limit_cost(input_rows: float, *, count: int, offset: int) -> Cost:
    """Free. It stops early; it does not do work.

    Whether stopping early *helps* depends on what is underneath: a limit above
    a sort saves nothing, because the sort had to see every row anyway. That is
    visible in the plan as a Limit whose child's cost did not fall.
    """
    return Cost(io=0.0, cpu=0.0, rows=min(input_rows, float(count + offset)) - offset)


def join_selectivity(
    left: TableStatistics, right: TableStatistics, *, equality: bool
) -> float:
    """The fraction of the cross product an equijoin is expected to keep.

    The textbook estimate, and the one PostgreSQL uses: for ``a.x = b.y``,

        selectivity = 1 / max(distinct(a.x), distinct(b.y))

    The reasoning is worth stating because the assumption inside it is what
    breaks. If every value of the *smaller* domain appears in the larger one (
    a foreign key, which is the common case) then each row of the larger side
    matches exactly one row of the smaller, and the result has as many rows as
    the larger side. Take the maximum of the two distinct counts and that falls
    out.

    Where it fails is skew. Ten million orders spread over three customers is
    still ``distinct = 3``, and the estimate is off by six orders of magnitude.
    Fixing that needs a most-common-values list, which is the single largest
    thing missing from this cost model.
    """
    if not equality:
        # A range join keeps a third of the cross product, on the same
        # reasoning as DEFAULT_INEQ_SELECTIVITY: an inequality is rarely
        # selective, and pretending otherwise makes a nested loop look cheap.
        return DEFAULT_INEQ_SELECTIVITY
    distinct = max(left.row_count, right.row_count, 1)
    return _clamp(1.0 / distinct)


def distinct_join_selectivity(left_distinct: int, right_distinct: int) -> float:
    """:func:`join_selectivity` when both columns really have been analyzed."""
    return _clamp(1.0 / max(left_distinct, right_distinct, 1))


def mcv_join_selectivity(
    left: ColumnStatistics,
    right: ColumnStatistics,
    left_rows: int,
    right_rows: int,
) -> float | None:
    """An equijoin's selectivity, read off both columns' most-common values.

    ``1 / max(distinct)`` assumes every value is equally common on both sides,
    and a join is exactly where that assumption compounds: an error in the
    estimate feeds every join above this one, so two tables wrong by 10x make a
    four-table plan wrong by 1,000.

    Matching the two lists removes the assumption for the values in them. This
    is PostgreSQL's ``eqjoinsel_inner`` in miniature, and it has three terms:

    1. **Both sides' lists agree on a value.** Its contribution is exactly
       ``f₁(v) · f₂(v)``, because both frequencies are counts.
    2. **A value in one list is missing from the other's.** It can still match
       rows in the other side's tail, spread over the distinct values that tail
       is known to hold.
    3. **Tail against tail**, uniform, which is the old estimate applied to what
       is left rather than to everything.

    The property worth noticing: when both lists are complete, terms 2 and 3 are
    zero and the first is the *true* join cardinality divided by the cross
    product. Not an estimate. Every foreign-key join between tables of ordinary
    size is in that case, which is the one Milestone 19 found being estimated at
    80x under its real size.

    ``None`` when either side has no list to match, which leaves the caller on
    :func:`distinct_join_selectivity`.
    """
    if not left.most_common or not right.most_common or not left_rows or not right_rows:
        return None

    left_frequency = {value: count / left_rows for value, count in left.most_common}
    right_frequency = {value: count / right_rows for value, count in right.most_common}
    shared = left_frequency.keys() & right_frequency.keys()

    matched = sum(left_frequency[value] * right_frequency[value] for value in shared)
    unmatched_left = sum(
        frequency for value, frequency in left_frequency.items() if value not in shared
    )
    unmatched_right = sum(
        frequency for value, frequency in right_frequency.items() if value not in shared
    )

    # The fraction of each side that neither list accounts for. NULLs are in
    # neither, and are excluded rather than left in the tail, because a NULL
    # never matches anything, not even another NULL.
    other_left = max(
        1.0 - left.null_fraction(left_rows) - left.most_common_rows / left_rows, 0.0
    )
    other_right = max(
        1.0 - right.null_fraction(right_rows) - right.most_common_rows / right_rows, 0.0
    )

    selectivity = matched
    tail_left = left.distinct_count - len(shared)
    tail_right = right.distinct_count - len(shared)
    if tail_right > 0:
        selectivity += unmatched_left * other_right / tail_right
    if tail_left > 0:
        selectivity += other_left * (unmatched_right + other_right) / tail_left
    return _clamp(selectivity)


# --------------------------------------------------------------------------
# Selectivity
# --------------------------------------------------------------------------


def estimate_selectivity(predicate: Expression | None, stats: TableStatistics) -> float:
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
            info = _column_stats(column, stats)
            if info is None:
                return DEFAULT_EQ_SELECTIVITY
            fraction = info.null_fraction(stats.row_count)
            return max(1.0 - fraction if negated else fraction, _one_row(stats.row_count))

        case BinaryOp() if predicate.operator.is_comparison:
            return _comparison_selectivity(predicate, stats)

    return DEFAULT_INEQ_SELECTIVITY


def _column_stats(
    column: BoundColumnRef, stats: TableStatistics
) -> ColumnStatistics | None:
    """Look a column up in its own table's statistics.

    By ``table_position``, **not** ``column_index``. Since Milestone 13 a bound
    index addresses the *joined* row, so ``sales.amount`` in
    ``customers JOIN sales`` is index 5, and asking a three-column table for
    its column 5 gets nothing. The statistics then fall back to a default, the
    filter's row estimate collapses, and the planner concludes a nested loop is
    nearly free. It did, for about ten minutes.

    A column with no ``table_position`` came from an aggregate's output row and
    has no table to be a position in.
    """
    if column.table_position is None:
        return None
    return stats.column(column.table_position)


def _comparison_selectivity(predicate: BinaryOp, stats: TableStatistics) -> float:
    matched = _as_column_literal(predicate)
    if matched is None:
        # `a < b` between two columns. PostgreSQL uses a fixed guess here too.
        return DEFAULT_INEQ_SELECTIVITY
    column, operator, value = matched

    info = _column_stats(column, stats)
    if info is None:
        return DEFAULT_EQ_SELECTIVITY

    if value is None:
        # `x = NULL` is UNKNOWN for every row in three-valued logic, so it
        # admits none. Not the same as `x IS NULL`, which is why the AST keeps
        # them as different nodes.
        return MIN_SELECTIVITY

    rows = stats.row_count
    non_null = 1.0 - info.null_fraction(rows)

    match operator:
        case BinaryOperator.EQ:
            fraction = _equality_fraction(info, value, rows)
        case BinaryOperator.NEQ:
            fraction = non_null - _equality_fraction(info, value, rows)
        case BinaryOperator.LT:
            fraction = _below_fraction(info, value, rows, inclusive=False)
        case BinaryOperator.LTE:
            fraction = _below_fraction(info, value, rows, inclusive=True)
        case BinaryOperator.GT:
            fraction = non_null - _below_fraction(info, value, rows, inclusive=True)
        case BinaryOperator.GTE:
            fraction = non_null - _below_fraction(info, value, rows, inclusive=False)
        case _:  # pragma: no cover - all comparisons are covered above
            return DEFAULT_INEQ_SELECTIVITY

    return max(fraction, _one_row(rows))


def _one_row(row_count: int) -> float:
    """The smallest a comparison's estimate may be: one row, never zero.

    Every number the estimator has is as of the last ``ANALYZE``, so "this value
    does not occur" means "did not occur when we looked". Predicting zero rows
    from that makes every operator above the scan free, and a plan built on a
    free subtree is not merely imprecise, it is unrecoverable: no amount of
    later work can outweigh nothing. PostgreSQL floors the same way in
    ``clamp_row_est``, for the same reason.

    ``x = NULL`` is deliberately *not* floored. It admits no rows by the
    definition of three-valued logic rather than by observation, which is a
    claim no future insert can falsify.
    """
    return 1.0 / row_count if row_count > 0 else MIN_SELECTIVITY


def _equality_fraction(info: ColumnStatistics, value: Any, row_count: int) -> float:
    """The fraction of rows equal to ``value``, from the most-common list.

    Three cases, and only the third is an estimate:

    * **In the list.** Its count is exact.
    * **Absent from a complete list.** The value does not occur, and the answer
      is zero rows. It is floored at *one* row instead, because the list is only
      complete as of the last ``ANALYZE``: a value inserted since is invisible
      to it, and an estimate of zero makes every operator above the scan look
      free. One row is the smallest honest answer, and it is what PostgreSQL
      clamps to as well.
    * **Absent from a partial list.** Uniform over what the list does not
      account for, which is the old estimate narrowed to the tail. Better than
      the old one, because the skewed head has been taken out of the average.
    """
    if row_count <= 0:
        return DEFAULT_EQ_SELECTIVITY
    for candidate, count in info.most_common:
        if candidate == value:
            return count / row_count
    if info.covers_every_value or _outside_range(info, value):
        return _one_row(row_count)
    if not info.most_common:
        return (1.0 - info.null_fraction(row_count)) / info.distinct_count

    tail_rows = row_count - info.null_count - info.most_common_rows
    tail_distinct = max(info.distinct_count - len(info.most_common), 1)
    return max(tail_rows, 0.0) / row_count / tail_distinct


def _outside_range(info: ColumnStatistics, value: Any) -> bool:
    """Whether ``value`` is beyond every value the column was seen to hold.

    Cheaper evidence than a complete most-common list and available far more
    often: min and max bound the column whatever its cardinality. Without it,
    ``bucket = 500`` on a column holding 0..99 gets the average of the tail,
    which is the estimate for a value that *might* be there. This one is not.
    """
    if info.minimum is None or info.maximum is None or not _comparable(info, value):
        return False
    return value < info.minimum or value > info.maximum


def _below_fraction(
    info: ColumnStatistics, value: Any, row_count: int, *, inclusive: bool
) -> float:
    """The fraction of rows below ``value``, counting MCVs and histogram buckets.

    The MCV part is exact: every listed value is either below the bound or not.
    The histogram part counts whole buckets and interpolates inside the one the
    bound falls in. Between them they replace a straight line drawn from min to
    max, which was wrong by however skewed the column was, and which could not
    tell ``<`` from ``<=`` at all.

    A column with neither structure (never analyzed, or analyzed when empty)
    falls back to that line, so :func:`_uniform_fraction` is still here.
    """
    if row_count <= 0:
        return DEFAULT_INEQ_SELECTIVITY
    if not info.most_common and not info.histogram:
        non_null = 1.0 - info.null_fraction(row_count)
        return non_null * _uniform_fraction(info, value)
    if not _comparable(info, value):
        return DEFAULT_INEQ_SELECTIVITY

    rows = sum(count for candidate, count in info.most_common if candidate < value)
    fraction = (rows + _histogram_rows_below(info, value, row_count)) / row_count
    if inclusive:
        # `<= v` is `< v` plus the rows equal to v, which is the one estimate
        # that already exists. A histogram cannot resolve a single value inside
        # a bucket, so without this `<` and `<=` come out identical, which is
        # the entire answer when the column holds two values. PostgreSQL splits
        # ``scalarltsel`` and ``scalarlesel`` the same way.
        fraction += _equality_fraction(info, value, row_count)
    return fraction


def _histogram_rows_below(info: ColumnStatistics, value: Any, row_count: int) -> float:
    """How many of the histogram's rows are below ``value``.

    Each bucket holds the same number of rows by construction, so a bound past
    ``k`` boundaries has ``k`` whole buckets below it. The straddled bucket is
    split by linear interpolation for a number, and taken as half for anything
    else: TEXT can be *ordered*, which is what puts the bound in the right
    bucket, but there is no sensible fraction of the way from 'apple' to 'pear'.
    """
    bounds = info.histogram
    if len(bounds) < 2:
        return 0.0
    covered = row_count - info.null_count - info.most_common_rows
    if covered <= 0:
        return 0.0

    buckets = len(bounds) - 1
    per_bucket = covered / buckets
    if value <= bounds[0]:
        return 0.0
    if value >= bounds[-1]:
        return float(covered)

    whole = 0
    while whole < buckets and value >= bounds[whole + 1]:
        whole += 1
    low, high = bounds[whole], bounds[whole + 1]
    if isinstance(value, (int, float)) and isinstance(low, (int, float)):
        span = float(high) - float(low)
        within = (float(value) - float(low)) / span if span > 0 else 0.5
    else:
        within = 0.5
    return (whole + min(max(within, 0.0), 1.0)) * per_bucket


def _comparable(info: ColumnStatistics, value: Any) -> bool:
    """Whether ``value`` can be ordered against this column's stored values.

    A boolean is not an integer here even though Python says otherwise, and the
    binder refuses the mixed comparison long before this, so the guard is about
    a statistics lookup landing on the wrong column rather than about SQL.
    """
    if info.data_type is DataType.BOOLEAN:
        return isinstance(value, bool)
    if info.data_type in (DataType.INTEGER, DataType.FLOAT):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def _uniform_fraction(info: ColumnStatistics, value: Any) -> float:
    """Where ``value`` falls between min and max, assuming a uniform spread.

    The estimate everything used before Milestone 20, kept for a column that has
    no summary at all. Uniformity is exactly what the histogram removes: on a
    column whose values cluster at one end this is wrong by however skewed the
    data is. It still gets the *shape* right, and a bound outside the observed
    range estimates ~0 or ~1, which is correct and is the case a fixed guess
    handles worst.
    """
    if not info.has_range or not isinstance(value, (int, float)):
        return DEFAULT_INEQ_SELECTIVITY

    low, high = float(info.minimum), float(info.maximum)
    point = float(value)
    if high <= low:
        # Every row shares one value: the predicate is all or nothing.
        return 1.0 if point > low else MIN_SELECTIVITY

    fraction = (point - low) / (high - low)
    return min(max(fraction, 0.0), 1.0)


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
