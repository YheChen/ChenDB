"""Tests of the tester.

This is the part worth refusing to ship without. A differential tester that has
quietly stopped comparing anything is green, fast, and worthless — and it looks
exactly like one that works. Every guard here exists to make one specific way of
going quiet loud instead:

* the generator drifting into all-empty or all-error output;
* a named corner (NULL group keys, empty groups, self-joins, orphaned join rows)
  silently ceasing to be generated;
* the case count collapsing, so the suite passes in fifty milliseconds;
* the oracle being weakened, by accident or by a well-meant "fix", until it agrees
  with everything.

The last one is the only guard that would catch ``compare()`` having been reduced
to ``return AGREE``, so it plants a difference of each kind the oracle claims to
detect and insists every one is reported.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Final

import pytest

from tests.differential import campaign, generator, shrink
from tests.differential.dialect import canonical, normalise, values_agree
from tests.differential.engines import Outcome
from tests.differential.generator import QUERIES_PER_CASE
from tests.differential.oracle import Verdict, compare
from tests.differential.test_differential import CI_SEEDS

#: Every corner the generator claims to reach. A weight is a hope; this is a
#: check. If one of these stops appearing, something upstream broke and the suite
#: would otherwise stay green while testing less.
REQUIRED_FEATURES: Final = (
    "null_group_key",
    "empty_group",
    "having",
    "self_join",
    "unmatched_join_row",
    "null_join_key",
    "outer_join",
    "left_join",
    "right_join",
    "full_join",
    "outer_join_extra_on",
    "order_by",
    "order_by_desc",
    "limit",
    "offset",
    "where",
    "boolean_predicate",
    "not_null_column",
    "index_present",
    "zero_row_table",
    "aggregate_over_empty_input",
    "dml_update",
    "dml_delete",
)


@pytest.fixture(scope="module")
def corpus() -> tuple[Counter[str], Counter[str], int]:
    """Feature and shape counts over the CI seeds. Generated, not executed.

    No engine runs here — the generator is deterministic, so what it *produces*
    can be measured without comparing anything, which keeps this guard fast enough
    to never be the reason someone skips the suite.
    """
    features: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    queries = 0
    for seed in CI_SEEDS:
        for query in generator.case(seed).queries:
            queries += 1
            shapes[query.shape] += 1
            for feature in query.features:
                features[feature] += 1
    return features, shapes, queries


# -- the generator has not gone quiet ----------------------------------------


def test_the_case_count_is_pinned(corpus):
    _, _, queries = corpus
    assert len(CI_SEEDS) == 64
    assert queries == len(CI_SEEDS) * QUERIES_PER_CASE


@pytest.mark.parametrize("feature", REQUIRED_FEATURES)
def test_the_corpus_hits_every_corner(corpus, feature: str):
    features, _, _ = corpus
    assert features[feature] >= 5, (
        f"{feature!r} appears {features[feature]} times across {len(CI_SEEDS)} "
        f"seeds. Either the generator stopped producing it — a coverage "
        f"regression that leaves the suite green — or the label is dead."
    )


def test_every_shape_is_generated(corpus):
    _, shapes, _ = corpus
    assert set(shapes) == {"scan", "aggregate", "grouped", "join", "self_join", "dml"}
    assert min(shapes.values()) >= 20


def test_the_corpus_is_not_trivial(tmp_path: Path):
    """Most queries must return rows, and almost all must run on both engines.

    A corpus where everything errors agrees perfectly and tests nothing; so does
    one where every query returns zero rows. Both are indistinguishable from
    success without a floor.
    """
    report = campaign.Report()
    for seed in CI_SEEDS[:16]:
        report.absorb(seed, campaign.run_case(generator.case(seed), tmp_path))

    assert report.compared >= 16 * QUERIES_PER_CASE
    both_ok = (
        report.verdicts[str(Verdict.AGREE)]
        + report.verdicts[str(Verdict.AGREE_WITHIN_TOLERANCE)]
    )
    assert both_ok / report.compared >= 0.85, (
        f"only {both_ok}/{report.compared} pairs ran on both engines:\n{report.render()}"
    )
    assert report.selects_with_rows / report.selects >= 0.3, (
        f"only {report.selects_with_rows}/{report.selects} SELECTs returned a row; "
        f"a corpus of empty results compares equal to anything"
    )


def test_a_generated_case_is_the_same_case_every_time():
    """The seed is the case's name, so it had better be a name.

    Anything non-deterministic in the generator — the clock, a set iteration, the
    module-level ``random`` — would make a CI failure unreproducible, which is the
    one thing that would make the whole suite useless rather than merely weaker.
    """
    first, second = generator.case(1234), generator.case(1234)
    assert [q.sql for q in first.queries] == [q.sql for q in second.queries]
    assert first.schema.setup(generator.CHENDB) == second.schema.setup(generator.CHENDB)
    assert generator.case(1235).queries[0].sql != first.queries[0].sql


def test_generated_sql_is_accepted_by_both_engines(tmp_path: Path):
    """The intersection of the two dialects, checked rather than assumed.

    A ``SQLITE_ONLY_ERROR`` means the generator has drifted outside SQLite's
    grammar and is silently testing less than it claims, which is why the oracle
    treats it as a harness failure and the registry cannot excuse it.
    """
    for seed in CI_SEEDS[:24]:
        for item in campaign.run_case(generator.case(seed), tmp_path):
            assert item.verdict is not Verdict.SQLITE_ONLY_ERROR, (
                f"seed {seed}: SQLite refused generated SQL:\n  {item.query.sqlite_sql}\n"
                f"  {item.theirs.error_class}: {item.theirs.error_message}"
            )


# -- the oracle has not been weakened ----------------------------------------


def _select(rows, columns=("c0",), **kwargs):
    return Outcome(ok=True, rows=tuple(rows), columns=columns, **kwargs)


def _query(**kwargs):
    defaults = {"sql": "SELECT 1", "sqlite_sql": "SELECT 1", "shape": "scan"}
    return generator.Query(**(defaults | kwargs))


def test_the_oracle_catches_a_planted_difference():
    """One planted difference of each kind the oracle claims to detect.

    This is the only guard that would notice ``compare()`` having been reduced to
    ``return AGREE``, which is what a weakened oracle looks like — and a weakened
    oracle is the failure mode with no other symptom.
    """
    plain = _query()
    planted = [
        ("a value", _select([(1,)]), _select([(2,)])),
        ("NULL vs 0", _select([(None,)]), _select([(0,)])),
        ("NULL vs empty string", _select([(None,)]), _select([("",)])),
        ("int vs float", _select([(2,)]), _select([(2.0,)])),
        ("a missing row", _select([(1,), (2,)]), _select([(1,)])),
        ("a duplicate row", _select([(1,), (1,)]), _select([(1,), (2,)])),
        ("a column count", _select([(1,)], ("c0",)), _select([(1, 2)], ("c0", "c1"))),
        ("a column name", _select([(1,)], ("c0",)), _select([(1,)], ("c9",))),
    ]
    for description, mine, theirs in planted:
        assert compare(plain, mine, theirs).verdict is Verdict.DIVERGE, (
            f"the oracle no longer reports a difference in {description}"
        )


def test_the_oracle_catches_a_row_moved_across_a_tie_boundary():
    """The clause that makes a tie-tolerant comparison a real test.

    Both results have the same sort-key sequence and the same row multiset. Only
    the *pairing* differs: ``(1, 'a')`` has moved from the first tie group to the
    second. Without the per-run check this passes, and a sort that shuffles rows
    between equal keys goes unnoticed.
    """
    ordered = _query(sort_key_indices=(0,))
    mine = _select([(1, "a"), (1, "b"), (2, "c")], ("c0", "c1"))
    theirs = _select([(1, "b"), (1, "c"), (2, "a")], ("c0", "c1"))
    assert compare(ordered, mine, theirs).verdict is Verdict.DIVERGE


def test_the_oracle_accepts_a_legitimate_tie_order():
    """The other half: with ties, a different row order is *not* a divergence.

    Both engines are entitled to it, and reporting it would make the suite red
    forever — the failure mode that gets a tester deleted.
    """
    ordered = _query(sort_key_indices=(0,))
    mine = _select([(1, "a"), (1, "b"), (2, "c")], ("c0", "c1"))
    theirs = _select([(1, "b"), (1, "a"), (2, "c")], ("c0", "c1"))
    assert compare(ordered, mine, theirs).verdict is Verdict.AGREE


def test_a_total_order_is_compared_as_a_sequence():
    total = _query(sort_key_indices=(0,), total_order=True)
    mine = _select([(1,), (2,)])
    theirs = _select([(2,), (1,)])
    assert compare(total, mine, theirs).verdict is Verdict.DIVERGE


def test_an_unordered_query_is_compared_as_a_multiset():
    assert compare(_query(), _select([(1,), (2,)]), _select([(2,), (1,)])).verdict is (
        Verdict.AGREE
    )


def test_the_float_tolerance_is_tight_and_reported():
    """A planted 1e-9 relative difference must still be reported.

    A tolerance is a place for a bug to hide, so it is worth pinning how much
    slack there actually is. And when the slack *is* load-bearing the verdict says
    so, rather than reporting a plain agreement.
    """
    tolerant = _query(tolerant_columns=frozenset({0}))
    assert compare(tolerant, _select([(1.0,)]), _select([(1.000000001,)])).verdict is (
        Verdict.DIVERGE
    )

    within = compare(tolerant, _select([(1.0,)]), _select([(1.0 + 1e-15,)]))
    assert within.verdict is Verdict.AGREE_WITHIN_TOLERANCE

    strict = _query()
    assert compare(strict, _select([(1.0,)]), _select([(1.0 + 1e-15,)])).verdict is (
        Verdict.DIVERGE
    ), "without the shape saying so, floats compare exactly"


def test_an_error_on_one_side_only_is_never_silently_accepted():
    plain = _query()
    failed = Outcome(ok=False, error_class="EvaluationError", error_message="boom")
    assert compare(plain, failed, _select([(1,)])).verdict is Verdict.CHENDB_ONLY_ERROR
    assert compare(plain, _select([(1,)]), failed).verdict is Verdict.SQLITE_ONLY_ERROR
    assert compare(plain, failed, failed).verdict is Verdict.AGREE


def test_dml_compares_the_rows_left_behind_and_not_only_the_count():
    """An UPDATE that reports the right count and writes the wrong value.

    The count is the cheap signal; the resulting state is the decisive one.
    """
    dml = _query(kind="update")
    mine = Outcome(ok=True, row_count=2, state=((1, 9), (2, 9)))
    theirs = Outcome(ok=True, row_count=2, state=((1, 9), (2, 8)))
    assert compare(dml, mine, theirs).verdict is Verdict.DIVERGE


# -- the comparison key agrees with the comparison ---------------------------


def test_the_canonical_key_never_separates_two_equal_values():
    """The invariant that ``-0.0`` broke, and that cost three false accusations.

    A multiset comparison sorts by :func:`canonical` and then walks the pairs with
    :func:`values_agree`. If the key is *finer* than the equality it serves, two
    equal values sort into different positions and the walk compares the wrong
    pairs — inventing a divergence out of a correct result.
    """
    for one, other in ((-0.0, 0.0), (0.0, -0.0), (1.5, 1.5), (True, 1), (False, 0)):
        if values_agree(normalise(one), normalise(other)):
            assert canonical((normalise(one),)) == canonical((normalise(other),)), (
                f"{one!r} and {other!r} compare equal but sort differently"
            )


def test_the_canonical_key_orders_mixed_types_without_raising():
    # A column can hold a NULL and a number, and Python refuses to compare them.
    rows = [(None,), (1,), ("a",), (2.5,), (True,)]
    assert len(sorted(rows, key=canonical)) == len(rows)


# -- shrinking ---------------------------------------------------------------


def test_shrinking_reduces_a_case_and_keeps_the_same_failure():
    """A planted failure: the last query of the case, and only that one.

    The shrinker must find it, drop the other fifteen queries, and stop — and the
    result must still fail *for the same reason*, which is the property that stops
    a shrinker wandering onto a different bug and reporting a minimal case for it.
    """
    case = generator.case(7)
    target_sql = case.queries[-1].sql

    def run(candidate):
        """Stand in for both engines: everything agrees except the planted query.

        The fake outcome has to be as wide as the query projects, or the oracle
        indexes a sort key past the end of the row — which is a property of a real
        outcome and so not something the oracle should have to defend against.
        """
        results = []
        for query in candidate.queries:
            width = max((*query.sort_key_indices, 0)) + 1
            agreeing = _select([tuple(range(width))], tuple(f"c{i}" for i in range(width)))
            outcome = (
                Outcome(ok=False, error_class="Planted", error_message="planted")
                if query.sql == target_sql
                else agreeing
            )
            results.append(compare(query, outcome, agreeing))
        return results

    signature = run(case)[-1].signature()
    smallest, steps = shrink.shrink(case, signature, run)

    assert len(smallest.queries) == 1
    assert smallest.queries[0].sql == target_sql
    assert shrink.size(smallest) < shrink.size(case)
    assert steps > 0
    assert any(item.signature() == signature for item in run(smallest) if item.fails)


def test_shrinking_gives_up_rather_than_changing_the_failure():
    """A case with nothing to reduce comes back unchanged, not broken."""
    case = generator.case(11)
    unreachable = ("diverge", "nothing", "nothing")
    smallest, _ = shrink.shrink(case, unreachable, lambda c: [], max_steps=25)
    assert smallest is case


def test_a_shrunk_case_still_renders_valid_sql(tmp_path: Path):
    """Reductions are on the spec, so the SQL is re-rendered rather than edited.

    That is what makes a shrunk case still a case: it runs, it can be shrunk
    again, and it pastes into a regression test. A shrinker that did string
    surgery on SQL would eventually emit something neither engine parses and then
    report *that* as the bug.
    """
    case = generator.case(3)
    for candidate in list(shrink.reductions(case))[:40]:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        try:
            for statement in candidate.schema.setup(generator.SQLITE):
                connection.executescript(statement)
        finally:
            connection.close()
