"""The no-em-dash rule, over the files a reader actually reads.

``visualizer/src/test/uiText.test.ts`` has enforced this since Milestone 12, and
it scans exactly one tree: ``visualizer/src``. So the rule held in tooltips and
nowhere else, and the gap was not theoretical. A README rewrite introduced 42 em
dashes with both suites green, because nothing was looking at Markdown or at
Python docstrings.

This is the same rule with the same reasoning, over the rest of the repository.

## What counts as a violation

An em dash **against a word** is prose punctuation: ``a — b`` or ``a—b``. Use a
comma, colon, semicolon, full stop or parentheses.

A **standalone** em dash is a placeholder for a value that is absent, and it
stays. ``formatBytes(NaN)`` returns one, an empty buffer frame shows one, and
the milestone tables in ``docs/roadmap.md`` put one in the "new page types"
column of a milestone that added none. The test is therefore about *adjacency*,
not about the character, which is what lets both live in one file:

    | 6 | — | (statistics are in memory)        allowed, a table cell
    │ 0      —    update    0     556 B  —     allowed, an ASCII diagram
    a cost model — calibrated by measurement   a violation

The middle case is why the rule allows more than one space on either side: a
column of dashes in a fixed-width diagram is a column of placeholders, and an
adjacency rule that ignored spacing would reject it while accepting nothing
useful in return.

## Scope

Markdown that a visitor reads, and Python that a reviewer reads. Not
``docs/openapi.json`` (generated), not ``.venv``, not ``__pycache__``, and not
``visualizer/src``, which has its own guard and its own reasons.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[2]

#: Trees to read, and the suffixes to read in them.
SCANNED: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("README.md", (".md",)),
    ("docs", (".md",)),
    ("engine", (".py",)),
    ("tests", (".py",)),
    ("examples", (".py",)),
    ("benchmarks", (".py",)),
    ("scripts", (".py",)),
)

SKIPPED_PARTS: Final = ("__pycache__", ".venv", "node_modules")

#: An em dash with at most one space between it and a word on either side.
#:
#: One space, not any, and that is the whole design of this pattern. Prose puts
#: at most one space around a dash; a fixed-width table puts several, because it
#: is aligning columns rather than writing a sentence. Backticks and brackets
#: count as word-adjacent so ``` `EXPLAIN` — the plan ``` is caught, but a quote
#: character does not, so ``per_event = "—"`` is not.
EM_DASH_IN_PROSE: Final = re.compile(r"[\w`)\]] ?—|— ?[\w`(\[]")

#: This file quotes the violations it forbids, in the docstring above and in the
#: cases below. Exempting it by name is honest and the alternative is not: a
#: guard that cannot describe what it rejects is a guard nobody can maintain.
EXEMPT: Final = frozenset({"tests/unit/test_prose_style.py"})


def _files() -> list[Path]:
    found: list[Path] = []
    for name, suffixes in SCANNED:
        target = ROOT / name
        if target.is_file():
            found.append(target)
            continue
        found.extend(
            path
            for path in sorted(target.rglob("*"))
            if path.is_file()
            and path.suffix in suffixes
            and not any(part in SKIPPED_PARTS for part in path.parts)
        )
    return [path for path in found if path.relative_to(ROOT).as_posix() not in EXEMPT]


def test_there_is_something_to_scan():
    """A guard that reads no files passes for the wrong reason."""
    files = _files()
    assert len(files) > 100, f"only {len(files)} files scanned; the globs are wrong"
    assert any(path.name == "README.md" for path in files), "README.md not scanned"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_em_dash_against_a_word(path: Path):
    offenders = [
        (number, line.strip())
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if EM_DASH_IN_PROSE.search(line)
    ]
    assert not offenders, "\n".join(
        [
            f"{path.relative_to(ROOT)} uses an em dash as prose punctuation.",
            "Use a comma, colon, semicolon, full stop or parentheses. A bare",
            "em dash as a placeholder for an absent value is fine.",
            *(f"  line {number}: {text}" for number, text in offenders[:5]),
        ]
    )


@pytest.mark.parametrize(
    "text",
    [
        "a cost model — calibrated by measurement",
        "slotted pages—the unit of storage",
        "`EXPLAIN` — names what it rejected",
        "the buffer pool —",
        "— and asserts both",
        "estimated (30) — measured (0.4 ms)",
    ],
)
def test_the_pattern_catches_prose(text: str):
    """Watched to fail, as a new guard has to be.

    Each of these is a shape the README actually contained before the rule was
    applied to it. A guard is not verified by passing; it is verified by
    rejecting the thing it exists to reject.
    """
    assert EM_DASH_IN_PROSE.search(text), f"missed a prose em dash: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "| 6 | — | (statistics are in memory, not persisted)",
        "│ 0      —    update    0     556 B  —      —      │",
        'per_event = "—"',
        "| `OFF` | 0.0450 s | 1.00x | 0 | — |",
        '<span className="text-muted">—</span>',
    ],
)
def test_the_pattern_allows_placeholders(text: str):
    """The other half, and the reason the rule is about adjacency.

    A rule that flagged every em dash would have to be turned off in the files
    that legitimately use one, which is how a guard stops being enforced.
    """
    assert not EM_DASH_IN_PROSE.search(text), f"false positive: {text!r}"
