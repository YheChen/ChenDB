"""The logical plan: *what* to compute, with no opinion on how.

Milestones 3 to 5 went straight from a bound statement to an operator tree, so
"the plan" and "the running query" were the same object.  That works until there
is more than one way to run something, at which point you need a form you can
rewrite and compare *before* committing to an implementation.

    AST  ──bind──▶  Logical  ──rewrite──▶  Logical'  ──cost──▶  Physical  ──▶  Operators
                       │                                            │
                  no index,                                    SeqScan or
                  no algorithm                                 IndexScan

The distinction is not bureaucracy.  ``LogicalScan(users)`` says "the rows of
users"; ``PhysicalSeqScan`` and ``PhysicalIndexScan`` are two ways to get them,
with different costs and identical results.  Keeping the first free of the
second is what lets a rewrite rule fire without knowing whether an index exists,
and what lets the cost model compare candidates that are, logically, the same
query.

PostgreSQL draws the same line between its ``Query`` (post-``transformStmt``)
and its ``Plan``; SQLite is the interesting counter-example — it goes almost
directly from parse tree to bytecode, and its query planner works by choosing
loops and indexes rather than by rewriting a tree.

Scope
-----
Three node types, because there are three operators.  No joins (one table per
``FROM``), no aggregation, no sorting — so the tree is always a chain, and
"plan enumeration" means choosing a leaf.  The moment a second table arrives,
this is where join order lives, and join order is where the real combinatorics
are: *n* tables have (2n-2)! / (n-1)! possible left-deep orders, which is why
PostgreSQL switches from exhaustive search to a genetic algorithm past
``geqo_threshold`` (12 by default).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.executor.expression import describe_expression
from engine.parser.ast import Expression
from engine.serialization.schema import Schema

if TYPE_CHECKING:
    from engine.executor.binder import ResultColumn

__all__ = [
    "LogicalFilter",
    "LogicalNode",
    "LogicalProject",
    "LogicalScan",
    "describe_logical",
    "walk_logical",
]


@dataclass(frozen=True, slots=True)
class LogicalNode:
    """One node of a logical plan.

    Frozen, like the AST, so a rewrite rule returns a new tree rather than
    mutating one someone else may be holding — which matters as soon as the
    planner wants to show what a plan looked like *before* a rule fired.
    """

    node_id: str

    @property
    def node_type(self) -> str:
        return type(self).__name__

    @property
    def children(self) -> tuple[LogicalNode, ...]:
        return ()

    @property
    def detail(self) -> str:
        return ""


@dataclass(frozen=True, slots=True)
class LogicalScan(LogicalNode):
    """Every row of one table. No commitment to *how* they are read."""

    table_name: str
    schema: Schema

    @property
    def detail(self) -> str:
        return f"table={self.table_name}"


@dataclass(frozen=True, slots=True)
class LogicalFilter(LogicalNode):
    """Keep the rows whose predicate is exactly TRUE."""

    predicate: Expression
    child: LogicalNode

    @property
    def children(self) -> tuple[LogicalNode, ...]:
        return (self.child,)

    @property
    def detail(self) -> str:
        return describe_expression(self.predicate)


@dataclass(frozen=True, slots=True)
class LogicalProject(LogicalNode):
    """Evaluate the select list."""

    projections: tuple[Expression, ...]
    output_columns: tuple[ResultColumn, ...]
    child: LogicalNode

    @property
    def children(self) -> tuple[LogicalNode, ...]:
        return (self.child,)

    @property
    def detail(self) -> str:
        return ", ".join(describe_expression(item) for item in self.projections)


def walk_logical(node: LogicalNode) -> list[LogicalNode]:
    """Every node, parents before children."""
    out = [node]
    for child in node.children:
        out.extend(walk_logical(child))
    return out


def describe_logical(node: LogicalNode, indent: int = 0) -> str:
    """Render a logical plan as text, for EXPLAIN and the docs."""
    detail = f"  {node.detail}" if node.detail else ""
    lines = [f"{'  ' * indent}{'└─ ' if indent else ''}{node.node_type}{detail}"]
    for child in node.children:
        lines.append(describe_logical(child, indent + 1))
    return "\n".join(lines)
