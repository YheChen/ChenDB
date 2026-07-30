"""The suite itself: one test per seed, so a failure names its own repro.

Sixty-four seeds of sixteen queries is about a thousand query pairs, which costs a
couple of seconds. ``CHENDB_DIFFERENTIAL_SEEDS`` widens it (``0:5000`` for a
nightly or a long local run) without a code change.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Final

import pytest

from tests.differential import campaign, generator, shrink
from tests.differential.dialect import MINIMUM_SQLITE_VERSION

#: Literal, not computed from a range or a date. A case's seed is its name, and a
#: name that changes with the calendar is not a name.
CI_SEEDS: Final[tuple[int, ...]] = tuple(range(64))


def _seeds() -> tuple[int, ...]:
    setting = os.environ.get("CHENDB_DIFFERENTIAL_SEEDS")
    if not setting:
        return CI_SEEDS
    start, _, stop = setting.partition(":")
    return tuple(range(int(start), int(stop))) if stop else (int(start),)


def test_sqlite_is_new_enough():
    """Fails rather than skips, deliberately.

    ``NULLS LAST`` needs SQLite 3.30, and without it every generated ``ORDER BY``
    would ask SQLite a different question than it asks ChenDB. The suite would
    stay green while comparing the wrong thing. ``sqlite3`` is in the standard
    library and cannot be absent, so there is nothing here to be lenient about.
    This is the ``CHENDB_REQUIRE_NODE`` lesson with no escape hatch needed: a
    guard that goes quiet is worse than no guard.
    """
    version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    assert version >= MINIMUM_SQLITE_VERSION, (
        f"SQLite {sqlite3.sqlite_version} cannot express NULLS LAST, so the "
        f"NULL-ordering translation would be a syntax error"
    )


@pytest.mark.parametrize("seed", _seeds())
def test_a_generated_case_agrees_with_sqlite(seed: int, tmp_path: Path):
    """One schema, sixteen queries, both engines, compared.

    The shrink runs *before* the failure is rendered, so the minimal case is in
    the CI log and nobody has to re-run anything to see what broke.
    """
    case = generator.case(seed)
    comparisons = campaign.run_case(case, tmp_path)
    failures = [item for item in comparisons if item.fails]
    if not failures:
        return

    target = failures[0].signature()
    smallest, steps = shrink.shrink(case, target, lambda c: campaign.run_case(c, tmp_path))
    again = [item for item in campaign.run_case(smallest, tmp_path) if item.fails]
    culprit = next((item for item in again if item.signature() == target), failures[0])
    pytest.fail(campaign.render_failure(smallest, culprit, seed=seed, steps=steps))
