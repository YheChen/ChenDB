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

What is collected, and what is not
----------------------------------
Four numbers per column: **distinct count**, **min**, **max**, and **null
count**.  That is enough for the three shapes of predicate the planner sees:

===================  ==============================  ==========================
Predicate            Estimate                        Needs
===================  ==============================  ==========================
``col = 5``          ``1 / distinct``                distinct count
``col < 5``          linear between min and max      min, max
``col IS NULL``      ``nulls / rows``                null count
===================  ==============================  ==========================

What is *not* collected is a **histogram**, and its absence is the single
biggest source of error here.  ``1 / distinct`` assumes every value is equally
common, so an index on a column where 90% of rows share one value will be
estimated as highly selective and chosen catastrophically.  PostgreSQL keeps a
most-common-values list *and* a histogram in ``pg_statistic`` precisely for this;
the equivalent here would be a list of the top *k* values with their frequencies,
and it is the first thing to add if the estimates start being wrong.

Exact, not sampled
------------------
``gather`` reads every row.  Real systems sample — PostgreSQL's
``default_statistics_target`` of 100 means it reads about 30,000 rows however
large the table is, because a full scan of a terabyte to refresh an *estimate*
is absurd.  ChenDB's tables are small enough that exactness is free, and being
exact removes one source of confusion when the estimates are wrong anyway.

Why these live in memory
------------------------
Statistics are **not persisted**.  They are gathered per open database and lost
on close.  That is a deliberate trade against a ``chendb_stats`` system table,
which would have meant format version 4:

* a statistic has no correctness consequence — a wrong one produces a slow
  query, never a wrong answer — and the file format should change for things
  that must survive, not for hints;
* the interesting failure here is **staleness**, and making statistics vanish on
  close makes their age impossible to ignore;
* PostgreSQL persists them because rescanning a terabyte at startup is
  impossible. That reason does not apply at this scale, and adopting the
  mechanism without the reason is cargo cult.

The cost is real: a freshly opened database plans its first query with nothing.
:meth:`StatisticsCatalog.for_table` therefore gathers lazily on first use, so a
plan is never made blind — and records ``gathered_after_writes`` so the API can
say how stale the numbers are.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.diagnostics.events import StatisticsGatheredEvent
from engine.diagnostics.tracer import NULL_TRACER, Tracer
from engine.serialization.record import decode_record, strip_tuple_header
from engine.serialization.types import DataType

if TYPE_CHECKING:
    from engine.database import Database

__all__ = [
    "ColumnStatistics",
    "StatisticsCatalog",
    "TableStatistics",
]

#: Assumed distinct count for a column that has never been analyzed. One means
#: "every row shares a value", which makes an equality predicate look
#: unselective and biases an unanalyzed table toward a sequential scan — the
#: safe direction, because a scan is never catastrophically wrong.
_UNKNOWN_DISTINCT = 1


@dataclass(frozen=True, slots=True)
class ColumnStatistics:
    """What is known about one column's contents."""

    name: str
    data_type: DataType
    distinct_count: int
    null_count: int
    minimum: Any = None
    maximum: Any = None

    def null_fraction(self, row_count: int) -> float:
        return self.null_count / row_count if row_count else 0.0

    @property
    def has_range(self) -> bool:
        """Whether min/max can support a range estimate.

        ``False`` for BOOLEAN and for a column that is entirely NULL. Ordering
        TEXT by min/max is possible but interpolating *between* two strings is
        not, so range estimates on TEXT fall back to the default.
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
    hooking every write path — which would couple the statistics module to the
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
        the memory cost — O(distinct) per column, which for a high-cardinality
        column is O(rows). A real system uses HyperLogLog or a sample precisely
        to bound that; the exact set is affordable here and removes any question
        of whether an estimate is wrong because of the sketch.
        """
        started = time.perf_counter_ns()
        info = self.database.require_table(name)
        heap = self.database.heap_for(info.name)

        seen: list[set[Any]] = [set() for _ in info.schema]
        nulls = [0] * len(info.schema)
        minima: list[Any] = [None] * len(info.schema)
        maxima: list[Any] = [None] * len(info.schema)
        row_count = 0

        for _, payload in heap.scan():
            row_count += 1
            # Every version, visible or not. Statistics describe what is on
            # disk, and the planner is costing page reads — which a dead
            # version costs just as much as a live one.
            row = decode_record(info.schema, strip_tuple_header(payload))
            for position, value in enumerate(row):
                if value is None:
                    nulls[position] += 1
                    continue
                seen[position].add(value)
                if minima[position] is None or value < minima[position]:
                    minima[position] = value
                if maxima[position] is None or value > maxima[position]:
                    maxima[position] = value

        columns = tuple(
            ColumnStatistics(
                name=column.name,
                data_type=column.data_type,
                distinct_count=max(len(seen[position]), _UNKNOWN_DISTINCT),
                null_count=nulls[position],
                minimum=minima[position],
                maximum=maxima[position],
            )
            for position, column in enumerate(info.schema)
        )

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
