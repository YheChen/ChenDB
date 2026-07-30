"""Running cases, and reporting what happened.

The reporting is not decoration. A generated failure is useless if a developer has
to re-derive it, so the shrink happens *before* the failure is rendered and the
minimal case goes straight into the CI log, schema, both dialects of the query,
the differing cell, and the two commands that reproduce it. Nothing has to be run
again to see what went wrong.

The counters exist for the opposite reason: to notice the suite going quiet. A
differential tester that has silently stopped comparing anything is green, fast,
and worthless, and it looks exactly like one that works. So every run prints how
many pairs it compared, how many returned rows, how often each registry entry
fired, and how many comparisons needed the float tolerance, and
``test_harness.py`` asserts floors on all of it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from tests.differential import registry
from tests.differential.engines import chendb_outcomes, sqlite_outcomes
from tests.differential.generator import CHENDB, SQLITE, Case
from tests.differential.oracle import Comparison, Verdict, compare

__all__ = ["Report", "render_failure", "run_case"]


@dataclass(slots=True)
class Report:
    """What a run of many cases measured. Printed whether it passed or failed."""

    verdicts: Counter[str] = field(default_factory=Counter)
    features: Counter[str] = field(default_factory=Counter)
    rules: Counter[str] = field(default_factory=Counter)
    shapes: Counter[str] = field(default_factory=Counter)
    selects: int = 0
    selects_with_rows: int = 0
    failures: list[tuple[int, Comparison]] = field(default_factory=list)

    def absorb(self, seed: int, comparisons: list[Comparison]) -> None:
        for item in comparisons:
            self.verdicts[str(item.verdict)] += 1
            self.shapes[item.query.shape] += 1
            for feature in item.query.features:
                self.features[feature] += 1
            if item.rule:
                self.rules[item.rule] += 1
            if item.query.kind == "select" and item.mine.ok:
                self.selects += 1
                if item.mine.rows:
                    self.selects_with_rows += 1
            if item.fails:
                self.failures.append((seed, item))

    @property
    def compared(self) -> int:
        return sum(self.verdicts.values())

    def render(self) -> str:
        lines = [f"compared {self.compared} query pairs"]
        for verdict, count in sorted(self.verdicts.items()):
            lines.append(f"  {verdict:<24} {count}")
        if self.selects:
            share = 100 * self.selects_with_rows / self.selects
            lines.append(f"  SELECTs returning rows   {share:.0f}%")
        lines.append(
            "  shapes: " + ", ".join(f"{k}={v}" for k, v in sorted(self.shapes.items()))
        )
        lines.append(
            "  registry: "
            + (
                ", ".join(f"{k}={v}" for k, v in sorted(self.rules.items()))
                or "nothing fired"
            )
        )
        return "\n".join(lines)


def run_case(instance: Case, workspace: Path) -> list[Comparison]:
    """Run one case on both engines and compare, query by query.

    A setup that fails on either engine raises rather than being reported as a
    divergence. It is a broken fixture, and letting it through would make every
    query in the case compare two empty databases and pass.
    """
    mine = chendb_outcomes(instance, workspace)
    theirs = sqlite_outcomes(instance)
    for run, engine in ((mine, CHENDB), (theirs, SQLITE)):
        if run.setup_failure is not None:
            raise AssertionError(
                f"the generated setup does not apply on {engine}, so nothing "
                f"below it was tested:\n  {run.setup_failure.statement}\n"
                f"  {run.setup_failure.error}"
            )

    comparisons: list[Comparison] = []
    for query, outcome, other in zip(
        instance.queries, mine.outcomes, theirs.outcomes, strict=True
    ):
        result = compare(query, outcome, other)
        if result.verdict is Verdict.CHENDB_ONLY_ERROR and (
            entry := registry.find(outcome, other)
        ):
            result = Comparison(
                query, outcome, other, Verdict.REGISTERED, result.detail, entry.rule
            )
        comparisons.append(result)
    return comparisons


def render_failure(instance: Case, failure: Comparison, *, seed: int, steps: int) -> str:
    """Everything needed to understand and reproduce one failure, in the log."""
    query = failure.query
    setup = "\n".join(instance.schema.setup(CHENDB))
    lines = [
        f"differential failure   seed={seed}  shape={query.shape}  "
        f"verdict={failure.verdict}  shrunk in {steps} steps",
        "",
        "-- schema " + "-" * 60,
        setup,
        "",
        "-- query " + "-" * 61,
        f"ChenDB:  {query.sql}",
    ]
    if query.sqlite_sql != query.sql:
        lines.append(f"SQLite:  {query.sqlite_sql}")
    lines.extend(["", "-- outcome " + "-" * 59, f"{failure.detail}"])

    if failure.mine.ok and failure.theirs.ok:
        comparison = (
            "exact sequence (the ORDER BY is total)"
            if query.total_order
            else "multiset, plus the sort-key sequence"
            if query.sort_key_indices
            else "multiset (no ORDER BY)"
        )
        lines.append(f"compared as: {comparison}")
        if query.kind == "select":
            lines.append(f"  ChenDB: {failure.mine.rows}")
            lines.append(f"  SQLite: {failure.theirs.rows}")
        else:
            lines.append(f"  ChenDB: {failure.mine.row_count} rows -> {failure.mine.state}")
            lines.append(
                f"  SQLite: {failure.theirs.row_count} rows -> {failure.theirs.state}"
            )
    else:
        lines.append(f"  ChenDB: {failure.mine.error_class}: {failure.mine.error_message}")
        lines.append(
            f"  SQLite: {failure.theirs.error_class}: {failure.theirs.error_message}"
        )

    lines.extend(
        [
            "",
            "-- reproduce " + "-" * 57,
            f".venv/bin/python scripts/differential.py --seed {seed} --verbose",
            f".venv/bin/python -m pytest 'tests/differential/test_differential.py"
            f"::test_a_generated_case_agrees_with_sqlite[{seed}]'",
        ]
    )
    return "\n".join(lines)
