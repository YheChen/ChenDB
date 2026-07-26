#!/usr/bin/env python3
"""A narrated tour of the Milestone 2 SQL front end.

    python examples/milestone2_parser.py

Shows tokens, the AST, operator precedence, and what happens when the SQL is
wrong. Nothing executes — that is Milestone 3.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.parser import analyze_sql, parse, tokenize, walk
from engine.parser.ast import Node

QUERY = "SELECT email, age * 2 AS doubled FROM users WHERE age >= 18 AND email IS NOT NULL"


def heading(number: int, text: str) -> None:
    print(f"\n\033[1m{number}. {text}\033[0m")
    print("─" * 78)


def show_tree(node: Node, sql: str, depth: int = 0) -> None:
    attributes = " ".join(
        f"{key}={value!r}"
        for key, value in node.attributes().items()
        if value not in (None, "", [], (), False)
    )
    prefix = "" if depth == 0 else f"{'│  ' * (depth - 1)}└─ "
    label = f"{prefix}{node.node_type}"
    print(f"  {label:<40} {attributes:<34} {node.text_in(sql)!r}")
    for child in node.children():
        show_tree(child, sql, depth + 1)


def main() -> int:
    # ------------------------------------------------------------------
    heading(1, "Tokens")
    print("   Every token knows the exact characters it came from.\n")
    print(f"   {'#':>2}  {'type':<16} {'span':<12} lexeme")
    for index, token in enumerate(tokenize(QUERY)):
        span = f"[{token.span.start}:{token.span.end}]"
        print(f"   {index:>2}  {token.type.value:<16} {span:<12} {token.lexeme!r}")

    # ------------------------------------------------------------------
    heading(2, "The abstract syntax tree")
    print(f"   {QUERY}\n")
    statement = parse(QUERY)[0]
    show_tree(statement, QUERY)
    print(f"\n   {len(walk(statement))} nodes. Every span slices back to real source,")
    print("   which is what lets the visualizer highlight a node's SQL.")

    # ------------------------------------------------------------------
    heading(3, "Precedence comes from the grammar's shape, not a table")
    for sql, explanation in [
        ("a = 1 OR b = 2 AND c = 3", "AND binds tighter than OR"),
        ("1 + 2 * 3 = 7", "* binds tighter than +, which binds tighter than ="),
        ("1 - 2 - 3 = 0", "left associative: (1 - 2) - 3"),
        ("NOT a = 1", "NOT applies to the whole comparison"),
    ]:
        where = parse(f"SELECT * FROM t WHERE {sql}")[0].where  # type: ignore[attr-defined]
        shape = _shape(where)
        print(f"   {sql:<26} → {shape:<28} {explanation}")

    # ------------------------------------------------------------------
    heading(4, "IS NULL is not an equality")
    print("   `x = NULL` is UNKNOWN for every input, including NULL. Keeping them")
    print("   distinct in the AST makes conflating them impossible downstream.\n")
    for sql in ("age IS NULL", "age IS NOT NULL", "age = NULL"):
        where = parse(f"SELECT * FROM t WHERE {sql}")[0].where  # type: ignore[attr-defined]
        print(f"   {sql:<20} → {where.node_type}")

    # ------------------------------------------------------------------
    heading(5, "Errors carry a position, and partial results survive")
    for sql in [
        "SELECT name FROM",
        "SELECT 'unterminated",
        "SELECT * FROM order",
        "SELECT * FROM t ORDER BY age",
    ]:
        outcome = analyze_sql(sql)
        assert outcome.error is not None
        print(f"   {sql!r}")
        print(
            f"     {type(outcome.error).__name__} at line {outcome.error.line}, "
            f"column {outcome.error.column}: {outcome.error.message}"
        )
        print(
            f"     still scanned {len(outcome.tokens)} token(s), "
            f"parsed {len(outcome.statements)} statement(s)"
        )

    # ------------------------------------------------------------------
    heading(6, "What the parser reported while doing all that")
    sink = RingBufferSink()
    parse(QUERY, tracer=Tracer(sink, TraceLevel.VERBOSE))
    counts: dict[str, int] = {}
    for item in sink.snapshot():
        counts[item.event_type] = counts.get(item.event_type, 0) + 1
    for event_type, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"   {event_type:<22} {count:>4}")
    created = [i for i in sink.snapshot() if i.event_type == "AstNodeCreatedEvent"]
    print("\n   Node events fire as rules complete, so the order is bottom-up:")
    print(f"     first  {created[0].event.node_type} (leaf)")
    print(f"     last   {created[-1].event.node_type} (root)")

    print("\n" + "─" * 78)
    print("Nothing above touched a page: Milestone 2 is purely syntactic.")
    print("Try it in the browser:  python -m engine.server  +  the SQL workspace")
    return 0


def _shape(node: Node) -> str:
    """A compact parenthesised rendering, to make precedence visible."""
    kids = node.children()
    label = node.attributes().get("operator") or node.node_type
    if not kids:
        value = node.attributes().get("name") or node.attributes().get("value")
        return str(value)
    if len(kids) == 1:
        return f"{label}({_shape(kids[0])})"
    return f"({_shape(kids[0])} {label} {_shape(kids[1])})"


if __name__ == "__main__":
    raise SystemExit(main())
