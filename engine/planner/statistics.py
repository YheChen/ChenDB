"""Table and column statistics: what the cost model reasons about.

A cost model cannot choose an access path without knowing how many rows a
predicate will match, and *that* cannot be known without knowing something about
the data.  This module is where "something" is defined.

    ANALYZE users
        │
        ├─ scan the heap once, decode every row
        ├─ per table:   row_count, page_count
        └─ per column:  distinct values, min, max, nulls
                             │
                             ▼
                    selectivity estimates  →  engine/optimizer/cost.py

What is collected
-----------------
Six things per column: **distinct count**, **min**, **max**, **null count**, a
**most-common-values list**, and a **histogram**.

===================  ==============================  ==========================
Predicate            Estimate                        Needs
===================  ==============================  ==========================
``col = 5``          the MCV's own frequency         most-common values
``col < 5``          MCVs below, plus buckets        most-common values, histogram
``col IS NULL``      ``nulls / rows``                null count
``a.x = b.y``        MCV against MCV                 both lists
===================  ==============================  ==========================

The first four were all there was until Milestone 20, and ``1 / distinct``
assumes every value is equally common.  On a column where 90% of rows share one
value, an index was estimated as highly selective and chosen catastrophically.
That assumption was named as the biggest source of error here for fourteen
milestones, and the note said the fix was "a list of the top *k* values with
their frequencies, the first thing to add if the estimates start being wrong".
They started being wrong: Milestone 19 found a foreign-key join estimated at 80x
under its real size.

Skew, and the size a summary has to be
--------------------------------------
The two structures divide the work the way PostgreSQL's ``pg_statistic`` does,
and for the same reason: **skew lives in the head of the distribution and shape
lives in the tail**.

* :attr:`ColumnStatistics.most_common` holds the top
  :data:`STATISTICS_TARGET` values with exact counts, so a frequent value is
  estimated exactly rather than averaged away.
* :attr:`ColumnStatistics.histogram` holds equi-depth bucket boundaries over
  everything *not* in that list, so a range predicate can count whole buckets
  instead of interpolating a straight line between min and max.

One property falls out of doing this exactly rather than by sampling, and it is
worth stating because it is unusual: when a column has no more distinct values
than the target, **the MCV list is the whole column**, and equality, inequality
and range estimates over it are not estimates at all. They are counts. Most
columns in a database this size are in that regime, including every foreign key
in the test fixtures.

Exact, not sampled
------------------
``gather`` reads every row.  Real systems sample, PostgreSQL's
``default_statistics_target`` of 100 means it reads about 30,000 rows however
large the table is, because a full scan of a terabyte to refresh an *estimate*
is absurd.  ChenDB's tables are small enough that exactness is free, and being
exact removes one source of confusion when the estimates are wrong anyway.

Why these live in memory
------------------------
Statistics are **not persisted**.  They are gathered per open database and lost
on close.  That is a deliberate trade against a ``chendb_stats`` system table,
which would have meant format version 4:

* a statistic has no correctness consequence (a wrong one produces a slow
  query, never a wrong answer) and the file format should change for things
  that must survive, not for hints;
* the interesting failure here is **staleness**, and making statistics vanish on
  close makes their age impossible to ignore;
* PostgreSQL persists them because rescanning a terabyte at startup is
  impossible. That reason does not apply at this scale, and adopting the
  mechanism without the reason is cargo cult.

The cost is real: a freshly opened database plans its first query with nothing.
:meth:`StatisticsCatalog.for_table` therefore gathers lazily on first use, so a
plan is never made blind, and records ``gathered_after_writes`` so the API can
say how stale the numbers are.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from engine.diagnostics.events import StatisticsGatheredEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.serialization.record import decode_record, strip_tuple_header
from engine.serialization.types import DataType

if TYPE_CHECKING:
    from engine.database import Database

__all__ = [
    "STATISTICS_TARGET",
    "ColumnStatistics",
    "StatisticsCatalog",
    "TableStatistics",
    "summarise",
]

#: Assumed distinct count for a column that has never been analyzed. One means
#: "every row shares a value", which makes an equality predicate look
#: unselective and biases an unanalyzed table toward a sequential scan, the
#: safe direction, because a scan is never catastrophically wrong.
_UNKNOWN_DISTINCT = 1

STATISTICS_TARGET: Final = 32
"""How many most-common values to keep, and how many histogram buckets.

PostgreSQL's ``default_statistics_target`` is 100 and controls the same two
things. Smaller here because ChenDB reads every row rather than sampling, so
the target bounds *memory in the summary* rather than sampling error, and
because a smaller number makes the boundary between "the list is complete" and
"the tail is estimated" easy to cross in a test. Raising it is free accuracy
and costs one tuple per value per column.
"""


@dataclass(frozen=True, slots=True)
class ColumnStatistics:
    """What is known about one column's contents."""

    name: str
    data_type: DataType
    distinct_count: int
    null_count: int
    minimum: Any = None
    maximum: Any = None
    most_common: tuple[tuple[Any, int], ...] = ()
    """The most frequent values with exact row counts, most frequent first.

    Where skew is answered. ``1 / distinct`` says every value is equally common,
    which is the assumption that makes a planner choose an index for a value
    held by 90% of the table. A value in this list is not estimated at all.
    """
    histogram: tuple[Any, ...] = ()
    """Equi-depth bucket boundaries over the values **not** in ``most_common``.

    ``n + 1`` boundaries delimit ``n`` buckets holding roughly equal numbers of
    rows, so resolution follows the data instead of the value range. Empty when
    the MCV list already covers every value, which is the common case here and
    means a range estimate is a count rather than an interpolation.
    """

    def null_fraction(self, row_count: int) -> float:
        return self.null_count / row_count if row_count else 0.0

    @property
    def most_common_rows(self) -> int:
        return sum(count for _, count in self.most_common)

    @property
    def covers_every_value(self) -> bool:
        """Whether ``most_common`` is the whole column rather than its head.

        When true, every non-NULL row is accounted for by an exact count, so a
        value absent from the list occurs zero times. That is a much stronger
        claim than an estimate, and the one place it can mislead is staleness:
        rows inserted since the last ``ANALYZE`` are invisible to it. The
        planner therefore floors an absent value at one row rather than zero,
        see :func:`~engine.optimizer.cost.estimate_selectivity`.
        """
        return bool(self.most_common) and len(self.most_common) == self.distinct_count

    @property
    def has_range(self) -> bool:
        """Whether min/max can support a range estimate.

        ``False`` for BOOLEAN and for a column that is entirely NULL. Ordering
        TEXT by min/max is possible but interpolating *between* two strings is
        not, so range estimates on TEXT fall back to the default. This is now
        the fallback rather than the mechanism: a column with statistics has a
        histogram, and only one with neither lands here.
        """
        return (
            self.minimum is not None
            and self.maximum is not None
            and self.data_type in (DataType.INTEGER, DataType.FLOAT)
        )


@dataclass(frozen=True, slots=True)
class TableStatistics:
    """A snapshot of one table, as of the moment it was analyzed."""

    table_name: str
    row_count: int
    page_count: int
    columns: tuple[ColumnStatistics, ...]
    gathered_at_ns: int
    writes_at_gather: int
    """The database's cumulative page-write count when this was taken.

    Comparing it against the current count is how staleness is detected without
    hooking every write path, which would couple the statistics module to the
    heap, the index and the catalog all at once.
    """

    def column(self, position: int) -> ColumnStatistics | None:
        return self.columns[position] if 0 <= position < len(self.columns) else None

    @property
    def rows_per_page(self) -> float:
        return self.row_count / self.page_count if self.page_count else 0.0


@dataclass(slots=True)
class StatisticsCatalog:
    """Per-database statistics, gathered on demand and cached in memory."""

    database: Database
    tracer: Tracer = NULL_TRACER
    _cache: dict[str, TableStatistics] = field(default_factory=dict)

    def for_table(self, name: str) -> TableStatistics:
        """Statistics for ``name``, gathering them if none have been taken.

        Lazily rather than never: a plan made with no statistics at all is a
        guess, and the first query after opening a database would be the one
        query guaranteed to get it wrong.
        """
        key = name.casefold()
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        return self.gather(name)

    def cached(self, name: str) -> TableStatistics | None:
        """What is already known, without gathering. For the API's stats view."""
        return self._cache.get(name.casefold())

    def is_stale(self, name: str) -> bool:
        """Whether the table has been written to since it was analyzed.

        Detected by comparing page-write counters rather than by invalidating on
        every write. A statistic that is *slightly* stale is still useful, and
        recomputing on each insert would cost a full scan per row.
        """
        stats = self._cache.get(name.casefold())
        if stats is None:
            return True
        return self.database.stats.page_writes != stats.writes_at_gather

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name.casefold(), None)

    def analyzed_tables(self) -> list[str]:
        return sorted(stats.table_name for stats in self._cache.values())

    def gather(self, name: str) -> TableStatistics:
        """Scan ``name`` and recompute everything. This is what ANALYZE runs.

        One pass, O(rows), building a set of seen values per column. That set is
        the memory cost, O(distinct) per column, which for a high-cardinality
        column is O(rows). A real system uses HyperLogLog or a sample precisely
        to bound that; the exact set is affordable here and removes any question
        of whether an estimate is wrong because of the sketch.
        """
        started = time.perf_counter_ns()
        info = self.database.require_table(name)
        heap = self.database.heap_for(info.name)

        # A Counter rather than a set, which is the whole of Milestone 20's
        # collection cost: the same O(distinct) entries per column, now with a
        # count in each. Everything below is derived from these without a
        # second pass over the heap.
        seen: list[Counter[Any]] = [Counter() for _ in info.schema]
        nulls = [0] * len(info.schema)
        minima: list[Any] = [None] * len(info.schema)
        maxima: list[Any] = [None] * len(info.schema)
        row_count = 0

        for _, payload in heap.scan():
            row_count += 1
            # Every version, visible or not. Statistics describe what is on
            # disk, and the planner is costing page reads: which a dead
            # version costs just as much as a live one.
            row = decode_record(info.schema, strip_tuple_header(payload))
            for position, value in enumerate(row):
                if value is None:
                    nulls[position] += 1
                    continue
                seen[position][value] += 1
                if minima[position] is None or value < minima[position]:
                    minima[position] = value
                if maxima[position] is None or value > maxima[position]:
                    maxima[position] = value

        columns = []
        for position, column in enumerate(info.schema):
            most_common, histogram = summarise(seen[position])
            columns.append(
                ColumnStatistics(
                    name=column.name,
                    data_type=column.data_type,
                    distinct_count=max(len(seen[position]), _UNKNOWN_DISTINCT),
                    null_count=nulls[position],
                    minimum=minima[position],
                    maximum=maxima[position],
                    most_common=most_common,
                    histogram=histogram,
                )
            )
        columns = tuple(columns)

        stats = TableStatistics(
            table_name=info.name,
            row_count=row_count,
            page_count=heap.page_count(),
            columns=columns,
            gathered_at_ns=time.time_ns(),
            writes_at_gather=self.database.stats.page_writes,
        )
        self._cache[info.name.casefold()] = stats

        if self.tracer.summary:
            self.tracer.emit(
                StatisticsGatheredEvent(
                    table_name=info.name,
                    row_count=row_count,
                    page_count=stats.page_count,
                    column_count=len(columns),
                    duration_ns=time.perf_counter_ns() - started,
                )
            )
        return stats

    def gather_all(self) -> list[TableStatistics]:
        """ANALYZE with no table named."""
        return [self.gather(info.name) for info in self.database.tables()]


# --------------------------------------------------------------------------
# Summarising one column
# --------------------------------------------------------------------------


def summarise(counts: Counter[Any]) -> tuple[tuple[tuple[Any, int], ...], tuple[Any, ...]]:
    """Split a column's value counts into a most-common list and a histogram.

    Two regimes, and which one a column is in decides whether the planner is
    estimating or counting:

    * **At most** :data:`STATISTICS_TARGET` **distinct values.** The list is the
      whole column and there is no histogram, because there is nothing left to
      describe. Equality, inequality and range predicates over such a column
      come out exact.
    * **More than that.** The top values go in the list, and the rest are
      summarised by equi-depth buckets. Skew is in the list, shape is in the
      buckets.

    Ties are broken by value rather than left to :meth:`Counter.most_common`,
    whose order among equal counts follows insertion. Statistics that depend on
    heap order would make a plan depend on the order rows were written in, and
    the same query would be planned differently after a ``VACUUM``.
    """
    if not counts:
        return (), ()

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if len(ordered) <= STATISTICS_TARGET:
        return tuple(ordered), ()

    most_common = tuple(ordered[:STATISTICS_TARGET])
    common = {value for value, _ in most_common}
    rest = sorted((value, count) for value, count in counts.items() if value not in common)
    return most_common, _equi_depth(rest, STATISTICS_TARGET)


def _equi_depth(rest: list[tuple[Any, int]], buckets: int) -> tuple[Any, ...]:
    """Boundaries splitting ``rest`` into buckets of roughly equal row counts.

    Equi-depth rather than equi-width, which is the choice that makes a
    histogram worth having. Equal-width buckets over a column whose values
    cluster at one end put almost every row in one bucket and describe the
    empty space in detail; equal-depth buckets spend their resolution where the
    rows are, which is what a selectivity estimate is asking about.

    The boundaries are strictly increasing, so a value common enough to span
    several buckets appears once and the buckets after it are simply thinner.
    Such a value is nearly always in the MCV list already: reaching here means
    it was not quite in the top :data:`STATISTICS_TARGET`.
    """
    total = sum(count for _, count in rest)
    if total == 0 or len(rest) < 2:
        return ()

    step = total / buckets
    bounds: list[Any] = [rest[0][0]]
    running = 0
    edge = 1
    for value, count in rest:
        running += count
        while edge < buckets and running >= edge * step:
            if value != bounds[-1]:
                bounds.append(value)
            edge += 1
    if rest[-1][0] != bounds[-1]:
        bounds.append(rest[-1][0])
    return tuple(bounds)
