#!/usr/bin/env python3
"""Generate TypeScript types from the FastAPI OpenAPI schema.

The API contract is defined once, in Pydantic.  Hand-maintaining a parallel set
of TypeScript interfaces guarantees drift: a renamed field compiles fine on
both sides and fails at runtime.  This script closes that loop.

    python scripts/generate_api_types.py

writes ``visualizer/src/types/api.ts`` and ``docs/openapi.json``.  The output is
committed, so the frontend builds without a running server, and CI can re-run
the script and fail if the result differs.

Only the subset of JSON Schema that FastAPI emits for these models is handled:
objects, arrays, ``$ref``, enums, string/number/boolean/null and ``anyOf``
unions. Anything unrecognised degrades to ``unknown`` rather than guessing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TS_OUTPUT = REPO_ROOT / "visualizer" / "src" / "types" / "api.ts"
SPEC_OUTPUT = REPO_ROOT / "docs" / "openapi.json"

HEADER = """\
/**
 * Generated from the engine's OpenAPI schema. Do not edit by hand.
 *
 *   python scripts/generate_api_types.py
 *
 * The source of truth is the Pydantic models in engine/server/schemas/.
 */

"""

#: JSON Schema primitives to TypeScript.
_PRIMITIVES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
}


def ts_type(schema: dict[str, Any]) -> str:
    """Render one JSON Schema node as a TypeScript type expression."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]

    if "anyOf" in schema or "oneOf" in schema:
        variants = schema.get("anyOf") or schema["oneOf"]
        rendered = sorted({ts_type(variant) for variant in variants})
        return " | ".join(rendered)

    if "const" in schema:
        return json.dumps(schema["const"])

    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])

    schema_type = schema.get("type")
    if schema_type == "array":
        return f"{ts_type(schema.get('items', {}))}[]"
    if schema_type == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {ts_type(extra)}>"
        return "Record<string, unknown>"
    if isinstance(schema_type, list):
        return " | ".join(_PRIMITIVES.get(item, "unknown") for item in schema_type)
    if schema_type in _PRIMITIVES:
        return _PRIMITIVES[schema_type]
    return "unknown"


def render_doc(schema: dict[str, Any], indent: str) -> list[str]:
    """Carry Pydantic ``description=`` text through as a JSDoc comment."""
    description = schema.get("description")
    if not description:
        return []
    lines = [line.strip() for line in description.strip().splitlines()]
    if len(lines) == 1:
        return [f"{indent}/** {lines[0]} */"]
    return [f"{indent}/**", *(f"{indent} * {line}" for line in lines), f"{indent} */"]


def render_interface(name: str, schema: dict[str, Any]) -> str:
    """Render one component schema as an exported interface or type alias."""
    if "enum" in schema and "properties" not in schema:
        values = " | ".join(json.dumps(value) for value in schema["enum"])
        return f"export type {name} = {values};\n"

    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))

    lines: list[str] = []
    lines.extend(render_doc(schema, ""))
    lines.append(f"export interface {name} {{")
    for prop_name, prop_schema in properties.items():
        lines.extend(render_doc(prop_schema, "  "))
        optional = "" if prop_name in required else "?"
        lines.append(f"  {json_key(prop_name)}{optional}: {ts_type(prop_schema)};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def json_key(name: str) -> str:
    """Quote a property name when it is not a bare identifier."""
    if name.isidentifier():
        return name
    return json.dumps(name)


def generate(spec: dict[str, Any]) -> str:
    components = spec.get("components", {}).get("schemas", {})
    blocks = [
        render_interface(name, schema)
        for name, schema in sorted(components.items())
        # FastAPI's built-in validation-error models add noise the client
        # never constructs; the uniform ApiError envelope is what we use.
        if name not in {"HTTPValidationError", "ValidationError"}
    ]
    return HEADER + "\n".join(blocks)


def main() -> int:
    from engine.server.app import create_app
    from engine.server.config import ServerConfig

    app = create_app(ServerConfig(workspace=REPO_ROOT / ".openapi-tmp"))
    spec = app.openapi()

    SPEC_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    SPEC_OUTPUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")

    TS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    TS_OUTPUT.write_text(generate(spec))

    schema_count = len(spec.get("components", {}).get("schemas", {}))
    print(f"wrote {SPEC_OUTPUT.relative_to(REPO_ROOT)}")
    print(f"wrote {TS_OUTPUT.relative_to(REPO_ROOT)} ({schema_count} schemas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
