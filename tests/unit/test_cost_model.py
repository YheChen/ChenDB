"""Statistics, selectivity, and the cost model.

The point of the whole milestone is one behaviour: **the planner picks the
cheaper access path**, where "cheaper" is measured, not assumed.  So the tests
that matter most are the crossover tests at the bottom. They pin down where the
index stops paying, and they fail if a change to the constants moves it.

Everything above them is the machinery that crossover depends on: statistics
that describe the data, and selectivity estimates that turn a predicate into a
row count. When the planner picks a bad plan, it is almost always a bad
selectivity estimate rather than bad arithmetic, which is why those get the most
cases.
"""

from __future__ import annotations

from collections import Counter
from itertools import pairwise

import pytest

from engine import Column, Database, DataType, Schema
from engine.executor.binder import bind_select
from engine.executor.engine import build_logical_plan, execute_script, plan_query
from engine.optimizer.cost import (
    CPU_TUPLE_COST,
    DEFAULT_INEQ_SELECTIVITY,
    PAGE_HIT_COST,
    PAGE_MISS_COST,
    Cost,
    distinct_pages_touched,
    estimate_selectivity,
    index_scan_cost,
    seq_scan_cost,
)
from engine.optimizer.rules import fold_constants_in
from engine.parser.parser import parse_statement
from engine.planner.physical import (
    DISABLE_COST,
    PhysicalIndexScan,
    PhysicalSeqScan,
    PlannerOptions,
)
from engine.planner.statistics import STATISTICS_TARGET, summarise

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("bucket", DataType.INTEGER),
    Column("label", DataType.TEXT, nullable=False),
)

ROWS = 2_000
BUCKETS = 100
PAGE_SIZE = 512


@pytest.fixture
def db(tmp_path):
    """2,000 rows, `bucket` uniform over 0..99, every 7th row's bucket NULL."""
    with Database.open(tmp_path / "cost.chendb", page_size=PAGE_SIZE) as handle:
        handle.create_table("t", SCHEMA)
        handle.insert_many(
            "t",
            [(n, None if n % 7 == 0 else n % BUCKETS, f"row{n:05d}") for n in range(ROWS)],
        )
        handle.create_index("t_bucket", "t", "bucket")
        yield handle


def selectivity_of(db: Database, where: str) -> float:
    """Estimate as the planner would, *after* the rewrite rules have run.

    Folding first is not incidental. ``-100`` parses as a unary negation of
    ``100``, not as a negative literal, so ``column < -100`` does not match the
    ``column <op> literal`` shape that both the estimator and the index planner
    look for until constant folding collapses it.
    """
    statement = parse_statement(f"SELECT id FROM t WHERE {where}")
    bound = bind_select(statement, db.catalog)
    assert bound.where is not None
    return estimate_selectivity(
        fold_constants_in(bound.where), db.statistics.for_table("t")
    )


def rows_matching(db: Database, where: str) -> int:
    """How many rows the predicate really admits. The number to compare against."""
    return execute_script(f"SELECT COUNT(*) FROM t WHERE {where}", db)[-1].rows[0][0]


def chosen_path(db: Database, sql: str, **kwargs) -> str:
    result = execute_script(sql, db, **kwargs)[0]
    assert result.planned is not None
    return next(a.access_path for a in result.planned.alternatives if a.chosen)


# -- statistics -------------------------------------------------------------


def test_statistics_describe_the_table(db: Database):
    stats = db.statistics.for_table("t")
    assert stats.row_count == ROWS
    assert stats.page_count > 1
    assert [column.name for column in stats.columns] == ["id", "bucket", "label"]


def test_distinct_counts_are_exact(db: Database):
    stats = db.statistics.for_table("t")
    assert stats.column(0).distinct_count == ROWS, "id is unique"
    # Every 7th row is NULL, so some bucket values may be entirely missing.
    assert stats.column(1).distinct_count <= BUCKETS


def test_nulls_are_counted_separately_from_values(db: Database):
    stats = db.statistics.for_table("t")
    bucket = stats.column(1)
    assert bucket.null_count == len([n for n in range(ROWS) if n % 7 == 0])
    assert bucket.null_fraction(stats.row_count) == pytest.approx(1 / 7, abs=0.01)


def test_min_and_max_ignore_nulls(db: Database):
    bucket = db.statistics.for_table("t").column(1)
    assert bucket.minimum == 0
    assert bucket.maximum == BUCKETS - 1


def test_statistics_are_gathered_lazily_rather_than_never(db: Database):
    # A plan made with no statistics at all is a guess, and the first query
    # after opening would be the one guaranteed to get it wrong.
    db.statistics.invalidate()
    assert db.statistics.cached("t") is None
    assert db.statistics.for_table("t").row_count == ROWS
    assert db.statistics.cached("t") is not None


def test_writing_marks_the_statistics_stale(db: Database):
    db.statistics.gather("t")
    assert not db.statistics.is_stale("t")
    db.insert("t", (99999, 5, "new"))
    assert db.statistics.is_stale("t"), "a write after ANALYZE must be visible"


def test_stale_statistics_are_still_used(db: Database):
    # Deliberate: a slightly stale estimate is far more useful than none, and
    # recomputing on every insert would cost a full scan per row. The staleness
    # is reported instead: see the EXPLAIN output and the API.
    db.statistics.gather("t")
    before = db.statistics.for_table("t").row_count
    db.insert_many("t", [(100_000 + n, 5, "x") for n in range(50)])
    assert db.statistics.for_table("t").row_count == before
    assert db.statistics.is_stale("t")


def test_analyze_refreshes_them(db: Database):
    db.statistics.gather("t")
    db.insert_many("t", [(100_000 + n, 5, "x") for n in range(50)])
    execute_script("ANALYZE t", db)
    assert db.statistics.for_table("t").row_count == ROWS + 50
    assert not db.statistics.is_stale("t")


def test_analyze_with_no_table_does_every_table(db: Database):
    db.create_table("other", Schema.of(Column("a", DataType.INTEGER)))
    db.statistics.invalidate()
    execute_script("ANALYZE", db)
    assert set(db.statistics.analyzed_tables()) == {"t", "other"}


# -- selectivity ------------------------------------------------------------


def test_equality_counts_the_value_rather_than_averaging_over_them(db: Database):
    """This used to assert ``non_null / distinct``, and that is now the contrast.

    ``bucket`` is ``n % 100`` except every seventh row, which is NULL, so bucket
    5 holds 17 of the 2,000 rows. The old formula predicts 17.14, which is very
    nearly right *because this fixture is nearly uniform*. The point is that the
    estimate is no longer a prediction: 17 is a count taken during ANALYZE.

    ``tests/unit/test_estimates.py`` has the fixture where the difference is 33x
    rather than 0.14 of a row.
    """
    stats = db.statistics.for_table("t")
    actual = rows_matching(db, "bucket = 5")
    assert actual == 17

    assert selectivity_of(db, "bucket = 5") == pytest.approx(actual / ROWS)

    averaged = (1 - stats.column(1).null_fraction(ROWS)) / stats.column(1).distinct_count
    assert averaged != pytest.approx(actual / ROWS), (
        "the old estimate should differ, or this fixture proves nothing"
    )


def test_a_value_that_never_occurs_is_estimated_at_one_row(db: Database):
    """Not zero, and the difference matters more than it looks.

    ``bucket`` holds 0..99, so 500 occurs nowhere and the most-common list plus
    the histogram between them say so exactly. Reporting zero would make every
    operator above the scan free, and a subtree costed at nothing wins every
    comparison it is ever part of. One row is the smallest honest answer, and it
    is also the safe one: the statistics are as of the last ANALYZE, and a value
    inserted since is invisible to them.
    """
    assert rows_matching(db, "bucket = 500") == 0
    assert selectivity_of(db, "bucket = 500") == pytest.approx(1 / ROWS)


def test_the_most_common_list_is_the_whole_column_when_it_is_small_enough(
    db: Database,
):
    """The regime that makes an estimate a count. Both sides of it are checked.

    ``label`` is unique across 2,000 rows and ``bucket`` has 100 distinct
    values, so neither fits in a list of :data:`STATISTICS_TARGET`. A column
    that does fit gets no histogram at all, because there is nothing left for
    one to describe.
    """
    stats = db.statistics.for_table("t")
    bucket = stats.column(1)
    assert bucket.distinct_count > STATISTICS_TARGET
    assert len(bucket.most_common) == STATISTICS_TARGET
    assert not bucket.covers_every_value
    assert bucket.histogram, "the tail needs a histogram"

    db.create_table("small", Schema.of(Column("flag", DataType.INTEGER)))
    db.insert_many("small", [(n % 3,) for n in range(90)])
    flag = db.statistics.gather("small").column(0)
    assert flag.covers_every_value
    assert flag.most_common == ((0, 30), (1, 30), (2, 30))
    assert flag.histogram == (), "nothing is left over to bucket"


def test_the_most_common_list_is_ordered_by_count_then_by_value(db: Database):
    """Ties are broken by value, not by whatever order the heap was scanned in.

    ``Counter.most_common`` breaks ties by insertion order, which here is heap
    order, which changes when rows are deleted and reinserted. A plan that
    depended on it would be planned differently after a VACUUM, and the same
    query would get a different plan for no reason anybody could see.
    """
    db.create_table("tied", Schema.of(Column("v", DataType.INTEGER)))
    db.insert_many("tied", [(v,) for v in (3, 1, 2, 3, 1, 2)])
    assert db.statistics.gather("tied").column(0).most_common == ((1, 2), (2, 2), (3, 2))


def test_the_histogram_describes_what_the_list_does_not(db: Database):
    """Equi-depth: strictly increasing bounds, over the non-MCV rows only."""
    bucket = db.statistics.for_table("t").column(1)
    listed = {value for value, _ in bucket.most_common}
    assert bucket.histogram == tuple(sorted(bucket.histogram))
    assert len(set(bucket.histogram)) == len(bucket.histogram), "strictly increasing"
    assert not (set(bucket.histogram) & listed), (
        "a value in the list is not in the histogram, or it would be counted twice"
    )


def test_equi_depth_buckets_hold_roughly_equal_row_counts():
    """The property that makes a histogram worth having, checked directly.

    Equal-*width* buckets over a column that clusters at one end put nearly
    every row in one bucket and spend their resolution describing empty space.
    Equal-*depth* buckets put it where the rows are, which is what a
    selectivity estimate is asking about. The values below are deliberately
    lopsided: 1,000 rows crammed into 0..9 and 100 spread over 100..1099.
    """
    counts = Counter(dict.fromkeys(range(10), 100))
    counts.update({100 + n: 1 for n in range(1000)})
    _, histogram = summarise(counts)

    assert histogram == tuple(sorted(histogram))
    assert histogram[0] >= 10, "the crammed values are all in the MCV list"

    widths = [b - a for a, b in pairwise(histogram)]
    assert max(widths) < 3 * (sum(widths) / len(widths)), (
        "equal depth over uniform tail values means near-equal widths too"
    )


def test_a_unique_column_is_estimated_as_one_row(db: Database):
    assert selectivity_of(db, "id = 500") == pytest.approx(1 / ROWS, rel=0.01)


def test_a_range_counts_values_below_the_bound(db: Database):
    # bucket spans 0..99, so `< 25` is about a quarter of the non-null rows.
    # It used to be a straight line drawn from min to max; it is now the
    # most-common values below the bound plus whole histogram buckets, which
    # gets the same answer here because this fixture is nearly uniform, and a
    # very different one where it is not.
    non_null = 1 - 1 / 7
    assert selectivity_of(db, "bucket < 25") == pytest.approx(0.25 * non_null, abs=0.03)
    assert selectivity_of(db, "bucket < 75") == pytest.approx(0.75 * non_null, abs=0.03)
    for bound in (5, 25, 50, 75, 95):
        assert selectivity_of(db, f"bucket < {bound}") == pytest.approx(
            rows_matching(db, f"bucket < {bound}") / ROWS, abs=0.02
        )


def test_a_range_can_tell_strict_from_inclusive(db: Database):
    # A straight line between min and max could not: `< 25` and `<= 25` were
    # the same number. The difference is one bucket's worth of rows, and it is
    # the whole answer when a column holds two distinct values.
    strict = selectivity_of(db, "bucket < 25")
    inclusive = selectivity_of(db, "bucket <= 25")
    assert inclusive > strict
    assert (inclusive - strict) * ROWS == pytest.approx(rows_matching(db, "bucket = 25"))


def test_a_bound_outside_the_observed_range_saturates(db: Database):
    # The case a fixed guess handles worst, and the reason min/max is worth
    # collecting even without a histogram.
    assert selectivity_of(db, "bucket < -100") < 0.01
    assert selectivity_of(db, "bucket > -100") > 0.8


def test_a_negative_literal_is_only_usable_after_folding(db: Database):
    # `-100` is UnaryOp(NEGATE, Literal(100)), which matches neither the
    # estimator's `column <op> literal` shape nor the index planner's. Constant
    # folding is what makes it one: so the rule is load-bearing for estimates,
    # not just a micro-optimisation.
    statement = parse_statement("SELECT id FROM t WHERE bucket < -100")
    bound = bind_select(statement, db.catalog)
    stats = db.statistics.for_table("t")
    assert estimate_selectivity(bound.where, stats) == pytest.approx(
        DEFAULT_INEQ_SELECTIVITY
    )
    assert estimate_selectivity(fold_constants_in(bound.where), stats) < 0.01


def test_greater_than_is_the_complement_of_less_than(db: Database):
    below = selectivity_of(db, "bucket < 30")
    above = selectivity_of(db, "bucket > 30")
    non_null = 1 - 1 / 7
    assert below + above == pytest.approx(non_null, abs=0.02)


def test_not_equal_is_almost_everything(db: Database):
    assert selectivity_of(db, "bucket <> 5") > 0.8


def test_and_multiplies_assuming_independence(db: Database):
    # The most consequential wrong assumption in any planner. Named in the
    # module docstring; asserted here so the behaviour is not accidental.
    first = selectivity_of(db, "bucket < 50")
    second = selectivity_of(db, "id < 1000")
    assert selectivity_of(db, "bucket < 50 AND id < 1000") == pytest.approx(
        first * second, rel=0.01
    )


def test_or_uses_inclusion_exclusion(db: Database):
    first = selectivity_of(db, "bucket = 5")
    second = selectivity_of(db, "bucket = 6")
    assert selectivity_of(db, "bucket = 5 OR bucket = 6") == pytest.approx(
        first + second - first * second, rel=0.01
    )


def test_is_null_uses_the_null_fraction(db: Database):
    assert selectivity_of(db, "bucket IS NULL") == pytest.approx(1 / 7, abs=0.01)
    assert selectivity_of(db, "bucket IS NOT NULL") == pytest.approx(6 / 7, abs=0.01)


def test_equality_against_null_admits_nothing(db: Database):
    # `x = NULL` is UNKNOWN for every row in three-valued logic. Not the same as
    # `x IS NULL`, which is exactly why the AST keeps them as different nodes.
    assert selectivity_of(db, "bucket = NULL") < 0.001


def test_a_comparison_between_two_columns_falls_back(db: Database):
    assert selectivity_of(db, "bucket < id") == pytest.approx(DEFAULT_INEQ_SELECTIVITY)


def test_no_predicate_admits_everything(db: Database):
    assert estimate_selectivity(None, db.statistics.for_table("t")) == 1.0


def test_selectivity_never_leaves_zero_to_one(db: Database):
    for where in ("bucket < 5000", "bucket > -5000", "bucket = 5 AND bucket = 6"):
        value = selectivity_of(db, where)
        assert 0.0 < value <= 1.0, where


# -- costing ----------------------------------------------------------------


def test_a_sequential_scan_costs_its_pages_and_its_rows(db: Database):
    stats = db.statistics.for_table("t")
    cost = seq_scan_cost(stats)
    assert cost.io == pytest.approx(stats.page_count * PAGE_MISS_COST)
    assert cost.cpu == pytest.approx(stats.row_count * CPU_TUPLE_COST)
    assert cost.rows == stats.row_count


def test_an_index_scan_is_dominated_by_its_heap_fetches(db: Database):
    # One heap read per matching row. Since Milestone 7 most of those are pool
    # hits rather than misses, so the marginal cost of a match sits between the
    # two: much closer to a hit once the scan is wide enough to have touched
    # every page already.
    stats = db.statistics.for_table("t")
    small = index_scan_cost(stats, matching_rows=10, height=3, entries_per_leaf=200)
    large = index_scan_cost(stats, matching_rows=1000, height=3, entries_per_leaf=200)
    per_extra_match = (large.io - small.io) / 990
    assert PAGE_HIT_COST <= per_extra_match <= PAGE_MISS_COST
    assert large.io > 20 * small.io, "cost must grow with matches, not stay flat"


def test_the_pool_is_visible_in_the_estimate(db: Database):
    # Without modelling hits, every fetch would be charged as a miss and a wide
    # index scan would look three times more expensive than it is.
    stats = db.statistics.for_table("t")
    wide = index_scan_cost(
        stats, matching_rows=stats.row_count, height=3, entries_per_leaf=200
    )
    all_misses = (3 + stats.row_count / 200 + stats.row_count) * PAGE_MISS_COST
    assert wide.io < all_misses * 0.6


def test_distinct_pages_saturates_at_the_page_count(db: Database):
    # Fetching far more rows than there are pages cannot touch more pages than
    # exist: the estimate has to know that or a wide scan is nonsense.
    stats = db.statistics.for_table("t")
    assert distinct_pages_touched(10 * stats.row_count, stats.page_count) == (
        pytest.approx(stats.page_count)
    )
    assert distinct_pages_touched(1, stats.page_count) == pytest.approx(1.0)
    assert distinct_pages_touched(0, stats.page_count) == 0.0


def test_a_point_lookup_costs_far_less_than_a_scan(db: Database):
    stats = db.statistics.for_table("t")
    point = index_scan_cost(stats, matching_rows=1, height=3, entries_per_leaf=200)
    assert point.total * 20 < seq_scan_cost(stats).total


def test_cost_splits_io_from_cpu(db: Database):
    cost = Cost(io=3.0, cpu=4.0, rows=10)
    assert cost.total == 7.0
    assert "io 3.0" in str(cost) and "cpu 4.0" in str(cost)


# -- the crossover: what the whole milestone is for -------------------------


def test_a_selective_predicate_chooses_the_index(db: Database):
    assert chosen_path(db, "SELECT id FROM t WHERE bucket = 5") == "PhysicalIndexScan"


def test_an_unselective_predicate_chooses_the_scan(db: Database):
    # Milestone 5 chose the index here and was 3.9x slower for it.
    assert chosen_path(db, "SELECT id FROM t WHERE bucket < 90") == "PhysicalSeqScan"


@pytest.mark.parametrize(
    ("cutoff", "expected"),
    [
        (1, "PhysicalIndexScan"),
        (5, "PhysicalIndexScan"),
        (90, "PhysicalSeqScan"),
        (99, "PhysicalSeqScan"),
    ],
)
def test_the_crossover_sits_where_the_measurements_say(
    db: Database, cutoff: int, expected: str
):
    """Pinned so a change to the constants cannot move it silently.

    ``benchmarks/index_vs_scan.py`` is where these boundaries came from: the
    index wins below roughly 14% selectivity on this engine and loses above it.
    """
    assert chosen_path(db, f"SELECT id FROM t WHERE bucket < {cutoff}") == expected


def test_a_predicate_on_an_unindexed_column_scans(db: Database):
    assert chosen_path(db, "SELECT id FROM t WHERE label = 'row00005'") == "PhysicalSeqScan"


def test_not_equal_never_uses_an_index(db: Database):
    # An index cannot bound `<>`: it would read the whole tree and then do a
    # random heap read per row: strictly worse than a scan, every time.
    assert chosen_path(db, "SELECT id FROM t WHERE bucket <> 5") == "PhysicalSeqScan"


def test_every_candidate_is_reported_with_a_reason(db: Database):
    result = execute_script("SELECT id FROM t WHERE bucket = 5", db)[0]
    alternatives = result.planned.alternatives
    assert len(alternatives) == 2
    chosen = [a for a in alternatives if a.chosen]
    rejected = [a for a in alternatives if not a.chosen]
    assert len(chosen) == 1
    assert rejected[0].rejected_because, "a loser must say why it lost"
    assert "cost of the chosen plan" in rejected[0].rejected_because


def test_the_chosen_plan_is_the_cheapest_one_offered(db: Database):
    for where in ("bucket = 5", "bucket < 50", "bucket < 95", "id = 7"):
        result = execute_script(f"SELECT id FROM t WHERE {where}", db)[0]
        alternatives = result.planned.alternatives
        winner = next(a for a in alternatives if a.chosen)
        assert winner.cost.total == min(a.cost.total for a in alternatives), where


# -- forcing a path ---------------------------------------------------------


def test_disabling_the_index_forces_a_scan(db: Database):
    path = chosen_path(
        db,
        "SELECT id FROM t WHERE bucket = 5",
        planner_options=PlannerOptions(enable_index_scan=False),
    )
    assert path == "PhysicalSeqScan"


def test_disabling_the_scan_forces_the_index(db: Database):
    path = chosen_path(
        db,
        "SELECT id FROM t WHERE bucket < 95",
        planner_options=PlannerOptions(enable_seq_scan=False),
    )
    assert path == "PhysicalIndexScan"


def test_a_disabled_path_is_penalised_not_removed(db: Database):
    # PostgreSQL's disable_cost trick: a query with every path disabled must
    # still produce a plan, so "off" is a strong preference, not a prohibition.
    result = execute_script(
        "SELECT id FROM t WHERE bucket = 5",
        db,
        planner_options=PlannerOptions(enable_seq_scan=False, enable_index_scan=False),
    )[0]
    assert result.planned is not None
    assert len(result.planned.alternatives) == 2
    assert all(a.cost.total >= DISABLE_COST for a in result.planned.alternatives)
    assert result.rows, "a plan still ran and returned rows"


def test_forcing_does_not_change_the_answer(db: Database):
    sql = "SELECT id FROM t WHERE bucket < 20"
    by_scan = execute_script(
        sql, db, planner_options=PlannerOptions(enable_index_scan=False)
    )[0]
    by_index = execute_script(
        sql, db, planner_options=PlannerOptions(enable_seq_scan=False)
    )[0]
    assert sorted(by_scan.rows) == sorted(by_index.rows)
    assert by_scan.rows, "the predicate must actually match something"


# -- the physical plan ------------------------------------------------------


def test_an_absorbed_predicate_leaves_no_filter(db: Database):
    # Milestone 5 always kept a Filter above the index scan. When the index
    # condition covers the whole predicate there is nothing left to check.
    statement = parse_statement("SELECT id FROM t WHERE bucket = 5")
    bound = bind_select(statement, db.catalog)
    planned = plan_query(bound, db)
    types = [node.node_type for node in _walk(planned.root)]
    assert "PhysicalIndexScan" in types
    assert "PhysicalFilter" not in types


def test_a_partly_absorbed_predicate_keeps_the_rest(db: Database):
    statement = parse_statement("SELECT id FROM t WHERE bucket = 5 AND label = 'row00005'")
    bound = bind_select(statement, db.catalog)
    planned = plan_query(bound, db)
    types = [node.node_type for node in _walk(planned.root)]
    assert "PhysicalIndexScan" in types
    assert "PhysicalFilter" in types


def test_the_logical_plan_has_no_opinion_on_access_paths(db: Database):
    statement = parse_statement("SELECT id FROM t WHERE bucket = 5")
    bound = bind_select(statement, db.catalog)
    logical = build_logical_plan(bound)
    rendered = str(logical)
    assert "Index" not in rendered and "SeqScan" not in rendered


def _walk(node):
    out = [node]
    for child in node.children:
        out.extend(_walk(child))
    return out


def test_physical_nodes_can_be_costed_without_touching_the_disk(db: Database):
    # A plan is data, not operators, which is what lets EXPLAIN cost a query it
    # never runs and lets the API serialise one outside the engine lock.
    statement = parse_statement("SELECT id FROM t WHERE bucket = 5")
    bound = bind_select(statement, db.catalog)
    db.statistics.for_table("t")  # gather first; that part does read
    before = db.stats.page_reads
    planned = plan_query(bound, db)
    assert planned.estimated_cost > 0
    # Opening the tree to ask its height is the one read planning still does.
    assert db.stats.page_reads - before <= 4


def test_seq_scan_and_index_scan_are_interchangeable(db: Database):
    statement = parse_statement("SELECT id, bucket FROM t WHERE bucket = 5")
    bound = bind_select(statement, db.catalog)
    by_scan = plan_query(bound, db, options=PlannerOptions(enable_index_scan=False))
    by_index = plan_query(bound, db, options=PlannerOptions(enable_seq_scan=False))
    scan_leaf = _walk(by_scan.root)[-1]
    index_leaf = _walk(by_index.root)[-1]
    assert isinstance(scan_leaf, PhysicalSeqScan)
    assert isinstance(index_leaf, PhysicalIndexScan)
    assert scan_leaf.schema == index_leaf.schema, "same rows, different algorithm"
