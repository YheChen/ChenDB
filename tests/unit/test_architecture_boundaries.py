"""Executable enforcement of the engine/server/frontend boundary.

These rules are the reason the engine stays embeddable and testable.  A comment
in a README does not survive contact with a deadline; an import scan does.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_ROOT = REPO_ROOT / "engine"
SERVER_ROOT = ENGINE_ROOT / "server"

#: Packages that must never be reachable from engine code outside the server.
WEB_PACKAGES = frozenset(
    {"fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core", "anyio"}
)

#: Third-party packages the engine core is allowed to import: none.
STDLIB_ONLY_MESSAGE = (
    "engine/ (outside engine/server/) must depend on the standard library only, "
    "so the engine can be embedded and tested with zero installed packages"
)


def engine_core_modules() -> list[Path]:
    """Every engine module except the server subpackage."""
    return sorted(
        path
        for path in ENGINE_ROOT.rglob("*.py")
        if SERVER_ROOT not in path.parents and path != SERVER_ROOT
    )


def imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize(
    "module", engine_core_modules(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_engine_core_never_imports_a_web_framework(module: Path):
    offenders = imported_roots(module) & WEB_PACKAGES
    assert not offenders, (
        f"{module.relative_to(REPO_ROOT)} imports {sorted(offenders)}. {STDLIB_ONLY_MESSAGE}."
    )


def test_engine_core_imports_only_stdlib_and_itself():
    import sys

    allowed = set(sys.stdlib_module_names) | {"engine"}
    violations: dict[str, set[str]] = {}
    for module in engine_core_modules():
        extra = imported_roots(module) - allowed
        if extra:
            violations[str(module.relative_to(REPO_ROOT))] = extra
    assert not violations, f"{STDLIB_ONLY_MESSAGE}. Offenders: {violations}"


def test_engine_never_imports_the_visualizer():
    for module in ENGINE_ROOT.rglob("*.py"):
        assert "visualizer" not in imported_roots(module), module


def test_diagnostic_events_do_not_know_how_to_serialize_themselves():
    # Serialization belongs at the API boundary (engine/server/mappers.py).
    # An event growing a `to_json` is the first step toward the frontend's
    # wire format leaking into storage code.
    from engine.diagnostics import events as events_module

    forbidden = {"to_json", "to_api", "json", "dict", "model_dump", "serialize"}
    for name in dir(events_module):
        candidate = getattr(events_module, name)
        if not isinstance(candidate, type):
            continue
        if not issubclass(candidate, events_module.DiagnosticEvent):
            continue
        leaked = forbidden & set(vars(candidate))
        assert not leaked, f"{candidate.__name__} defines {leaked}"


@pytest.mark.skipif(not SERVER_ROOT.exists(), reason="server not built yet")
def test_the_server_depends_on_the_engine_and_not_the_other_way_round():
    for module in SERVER_ROOT.rglob("*.py"):
        # The server may import anything; the point is the reverse direction.
        assert module.is_file()

    for module in engine_core_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            imported = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                imported = node.module
            elif isinstance(node, ast.Import):
                imported = ",".join(alias.name for alias in node.names)
            assert "engine.server" not in imported, (
                f"{module.relative_to(REPO_ROOT)} imports engine.server; "
                f"the dependency must point the other way"
            )


def test_importing_the_engine_pulls_in_no_third_party_package():
    """Proof by observation rather than by reading imports.

    A transitive dependency three modules deep would slip past the AST scan
    above; loading the package in a clean interpreter and inspecting
    ``sys.modules`` will not miss it.
    """
    import json
    import subprocess
    import sys

    program = (
        "import sys, json;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "import engine;"
        f"loaded = sorted({sorted(WEB_PACKAGES)!r}) ;"
        "print(json.dumps({"
        "'version': engine.__version__,"
        "'leaked': sorted(set(loaded) & set(sys.modules)),"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["version"]
    assert payload["leaked"] == [], (
        f"importing engine loaded {payload['leaked']}. {STDLIB_ONLY_MESSAGE}."
    )


# -- one milestone number ---------------------------------------------------
#
# It used to be written out three times — engine/__init__, the CLI banner and
# the server's /health — and the CLI's copy sat a whole release behind, because
# a stale string breaks nothing. Now they all derive from the version, and these
# tests are what stop the derivation quietly coming apart again.


def test_the_milestone_is_derived_from_the_version():
    """``0.N.0`` is Milestone N, and ``1.0.0`` is Milestone 10.

    The roadmap's rule runs out at ten, because there is no ``0.10.0`` that
    sorts after ``0.9.0``. The arithmetic carries it across the boundary rather
    than special-casing it.
    """
    import engine

    major, minor, _ = (int(part) for part in engine.__version__.split("."))
    assert major * 10 + minor == engine.MILESTONE


def test_the_packaged_version_matches_the_engine():
    import tomllib

    import engine

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == engine.__version__


def test_the_feature_list_never_outruns_the_milestone():
    import engine

    # It was `==` for eleven milestones. Milestone 12 shipped CI, which is a
    # guarantee about the engine rather than something the engine can do, and
    # "storage + SQL + … + CI" is not a sentence a banner should print. So the
    # list is allowed to lag — but never to lead, which would mean a feature
    # was announced before it existed.
    assert len(engine.MILESTONE_FEATURES) <= engine.MILESTONE
    assert len(engine.MILESTONE_FEATURES) >= engine.MILESTONE - 1, (
        "the CLI banner enumerates what shipped; a missing entry usually means "
        "a milestone landed without the banner being told"
    )


def test_the_api_reports_the_engine_milestone():
    import engine
    from engine.server import app

    assert app.MILESTONE == engine.MILESTONE
