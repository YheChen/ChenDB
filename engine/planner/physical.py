"""Physical planning: enumerate the ways to run a logical plan, then cost them.

This is the module Milestone 5 was missing.  Its planner picked an index
whenever one covered a comparison, which is right below about 14% selectivity
and wrong — by 3.8x — above it.  Here the same candidates are *generated*, each
is costed against real statistics, and the cheapest wins.

    LogicalScan(users)  +  WHERE bucket < 700
            │
            ├── PhysicalSeqScan            cost 3233   ← chosen
            └── PhysicalIndexScan(bucket)  cost 11553   rejected: 3.6x the cost
                                                        of a sequential scan

Every candidate is kept, costed, and reported — not just the winner.  A planner
that shows only its answer is unarguable; one that shows what it rejected and
why can be checked, and is the difference between a plan view that teaches and
one that decorates.

The shape of the search
-----------------------
With one table and no joins, "enumeration" means listing access paths: a
sequential scan, plus one index scan per index that covers part of the
predicate.  That is *k+1* candidates for *k* indexes, and picking the minimum is
a loop.

That is not what makes planning hard.  Join order is: *n* tables admit
``(2n-2)! / (n-1)!`` left-deep orders — 30,240 for six tables, 17 billion for
ten — which is why PostgreSQL enumerates exhaustively only below
``geqo_threshold`` (12 relations) and switches to a genetic algorithm above it,
and why System R's 1979 dynamic-programming approach is still the reference.
None of that is needed here, and pretending otherwise would be architecture for
a problem this milestone does not have.

Physical nodes are data, not operators
--------------------------------------
A ``PhysicalIndexScan`` holds the index name and the key bounds; it does not
hold a ``BPlusTree``.  Turning it into a running operator is a separate step
(:func:`engine.executor.engine.materialise`), which buys three things: a plan
can be costed and compared without opening anything, ``EXPLAIN`` can print a
plan it never runs, and the API can serialise one without holding the engine
lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from engine.errors import IndexingError
from engine.executor.binder import BoundColumnRef, ResultColumn
from engine.executor.expression import describe_expression
from engine.index.key import SMALLEST_VALUE_KEY, describe_key
from engine.optimizer.cost import (
    Cost,
    estimate_selectivity,
    filter_cost,
    index_scan_cost,
    project_cost,
    seq_scan_cost,
)
from engine.optimizer.rules import apply_rules
from engine.parser.ast import BinaryOp, BinaryOperator, Expression, Literal
from engine.planner.logical import (
    LogicalFilter,
    LogicalNode,
    LogicalProject,
    LogicalScan,
)
from engine.planner.statistics import TableStatistics
from engine.serialization.schema import Schema

if TYPE_CHECKING:
    from engine.catalog.catalog import IndexInfo
    from engine.database import Database

__all__ = [
    "DISABLE_COST",
    "Alternative",
    "PhysicalFilter",
    "PhysicalIndexScan",
    "PhysicalNode",
    "PhysicalProject",
    "PhysicalSeqScan",
    "PlannedQuery",
    "PlannerOptions",
    "describe_physical",
    "plan_select",
    "walk_physical",
]

#: What a disabled access path costs. Enormous, but finite: PostgreSQL uses the
#: same trick (``disable_cost``) rather than removing the path, because a query
#: with *every* path disabled must still produce a plan. Turning an option off is
#: a strong preference, not a prohibition.
DISABLE_COST: Final = 1e10

#: Entries per leaf when the real figure is unknown. Only affects how many leaf
#: pages a range scan is charged for, which is a small term next to the heap
#: fetches — so an estimate is fine, and reading the tree to find out would make
#: costing do I/O.
_ASSUMED_ENTRIES_PER_LEAF = 200.0


# --------------------------------------------------------------------------
# Physical nodes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhysicalNode:
    """One node of a physical plan: an algorithm, plus what it will cost."""

    node_id: str
    estimated: Cost

    @property
    def node_type(self) -> str:
        return type(self).__name__

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return ()

    @property
    def detail(self) -> str:
        return ""

    @property
    def total_cost(self) -> float:
        """This node plus everything below it — what a comparison uses."""
        return self.estimated.total + sum(child.total_cost for child in self.children)


@dataclass(frozen=True, slots=True)
class PhysicalSeqScan(PhysicalNode):
    """Read every page of the heap, in physical order."""

    table_name: str
    schema: Schema

    @property
    def detail(self) -> str:
        return f"table={self.table_name}"


@dataclass(frozen=True, slots=True)
class PhysicalIndexScan(PhysicalNode):
    """Descend a B+ tree, then fetch each matching row from the heap."""

    table_name: str
    schema: Schema
    index_name: str
    low: bytes | None
    high: bytes | None
    include_low: bool
    include_high: bool
    condition: str
    """The index condition, rendered. PostgreSQL's ``Index Cond``."""

    @property
    def detail(self) -> str:
        return f"index={self.index_name} {self.condition}"


@dataclass(frozen=True, slots=True)
class PhysicalFilter(PhysicalNode):
    """Evaluate a predicate per row. PostgreSQL's ``Filter``, not ``Index Cond``."""

    predicate: Expression
    child: PhysicalNode

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.child,)

    @property
    def detail(self) -> str:
        return describe_expression(self.predicate)


@dataclass(frozen=True, slots=True)
class PhysicalProject(PhysicalNode):
    """Evaluate the select list."""

    projections: tuple[Expression, ...]
    output_columns: tuple[ResultColumn, ...]
    child: PhysicalNode

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.child,)

    @property
    def detail(self) -> str:
        return ", ".join(describe_expression(item) for item in self.projections)


# --------------------------------------------------------------------------
# The result of planning
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannerOptions:
    """Switches that let a caller override the cost model.

    The equivalent of PostgreSQL's ``enable_seqscan`` / ``enable_indexscan``,
    and they exist for the same two reasons: proving what the planner *would*
    have done, and letting a benchmark measure the path that was not chosen.
    A disabled path is penalised, not removed — see :data:`DISABLE_COST`.
    """

    enable_seq_scan: bool = True
    enable_index_scan: bool = True

    @property
    def all_enabled(self) -> bool:
        return self.enable_seq_scan and self.enable_index_scan


DEFAULT_PLANNER_OPTIONS: Final = PlannerOptions()


@dataclass(frozen=True, slots=True)
class Alternative:
    """One access path the planner considered.

    ``rejected_because`` is populated for every loser. A planner that says only
    what it chose cannot be argued with; one that says what it turned down, and
    by how much, can be checked against reality.
    """

    description: str
    access_path: str
    cost: Cost
    chosen: bool
    rejected_because: str = ""
    index_name: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    """Everything planning produced, including the parts it did not use."""

    root: PhysicalNode
    logical: LogicalNode
    rewrites: tuple[str, ...]
    alternatives: tuple[Alternative, ...]
    statistics: TableStatistics
    statistics_are_stale: bool

    @property
    def estimated_cost(self) -> float:
        return self.root.total_cost

    @property
    def estimated_rows(self) -> float:
        return self.root.estimated.rows


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def plan_select(
    logical: LogicalNode,
    database: Database,
    options: PlannerOptions = DEFAULT_PLANNER_OPTIONS,
) -> PlannedQuery:
    """Rewrite, enumerate, cost, and pick.

    The four steps a textbook names, in order, each observable on its own: the
    rewrites that fired, the candidates that existed, what each cost, and which
    won.
    """
    rewritten = apply_rules(logical)
    scan = _find_scan(rewritten.plan)
    stats = database.statistics.for_table(scan.table_name)
    stale = database.statistics.is_stale(scan.table_name)

    predicate = _predicate_of(rewritten.plan)
    candidates = _enumerate(scan, predicate, database, stats, options)
    chosen = min(candidates, key=lambda candidate: candidate.leaf.total_cost)

    alternatives = tuple(
        Alternative(
            description=candidate.description,
            access_path=candidate.leaf.node_type,
            cost=candidate.leaf.estimated,
            chosen=candidate is chosen,
            rejected_because=(
                ""
                if candidate is chosen
                else _why_rejected(candidate.leaf.total_cost, chosen.leaf.total_cost)
            ),
            index_name=candidate.index_name,
        )
        for candidate in candidates
    )

    root = _build(rewritten.plan, chosen, stats, _Namer())
    return PlannedQuery(
        root=root,
        logical=rewritten.plan,
        rewrites=rewritten.applied,
        alternatives=alternatives,
        statistics=stats,
        statistics_are_stale=stale,
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A leaf, plus what it absorbed from the predicate."""

    leaf: PhysicalNode
    description: str
    absorbed: frozenset[int]
    """Indices into the conjunct list this leaf handles itself."""
    index_name: str | None = None


def _enumerate(
    scan: LogicalScan,
    predicate: Expression | None,
    database: Database,
    stats: TableStatistics,
    options: PlannerOptions,
) -> list[_Candidate]:
    """Every way to read the table. Always at least the sequential scan."""
    candidates = [
        _Candidate(
            leaf=PhysicalSeqScan(
                node_id="scan_1",
                estimated=_penalise(seq_scan_cost(stats), options.enable_seq_scan),
                table_name=scan.table_name,
                schema=scan.schema,
            ),
            description=f"Sequential scan of {scan.table_name}"
            + ("" if options.enable_seq_scan else " (disabled)"),
            absorbed=frozenset(),
        )
    ]
    if predicate is None:
        return candidates

    conjuncts = _split_conjunction(predicate)
    positions_by_column: dict[int, list[int]] = {}
    for position, conjunct in enumerate(conjuncts):
        matched = _as_column_comparison(conjunct)
        if matched is not None:
            positions_by_column.setdefault(matched[0].column_index, []).append(position)

    for column_position, conjunct_positions in positions_by_column.items():
        for info in database.catalog.indexes_on(scan.table_name, column_position):
            bounds = _bounds_for(
                [(position, conjuncts[position]) for position in conjunct_positions],
                info,
            )
            if bounds is None:
                continue
            low, high, include_low, include_high, absorbed = bounds

            absorbed_selectivity = estimate_selectivity(
                _rebuild_conjunction([conjuncts[position] for position in sorted(absorbed)]),
                stats,
            )
            matching = max(stats.row_count * absorbed_selectivity, 1.0)
            tree = database.tree_for(info.name)
            condition = _describe_condition(
                low, high, include_low, include_high, info
            )
            candidates.append(
                _Candidate(
                    leaf=PhysicalIndexScan(
                        node_id="scan_1",
                        estimated=_penalise(
                            index_scan_cost(
                                stats,
                                matching_rows=matching,
                                height=tree.height,
                                entries_per_leaf=_ASSUMED_ENTRIES_PER_LEAF,
                            ),
                            options.enable_index_scan,
                        ),
                        table_name=scan.table_name,
                        schema=scan.schema,
                        index_name=info.name,
                        low=low,
                        high=high,
                        include_low=include_low,
                        include_high=include_high,
                        condition=condition,
                    ),
                    description=f"Index scan on {info.name} ({condition})"
                    + ("" if options.enable_index_scan else " (disabled)"),
                    absorbed=frozenset(absorbed),
                    index_name=info.name,
                )
            )
    return candidates


def _penalise(cost: Cost, enabled: bool) -> Cost:
    return cost if enabled else Cost(io=cost.io + DISABLE_COST, cpu=cost.cpu, rows=cost.rows)


def _why_rejected(cost: float, winner: float) -> str:
    if cost >= DISABLE_COST:
        return "disabled by planner options"
    if winner <= 0:
        return "the chosen plan is free"  # pragma: no cover - costs are positive
    ratio = cost / winner
    return f"{ratio:.1f}x the cost of the chosen plan"


class _Namer:
    """Operator ids, numbered per type: filter_1, filter_2, project_1.

    Per type rather than globally so an id says what the node is, and so adding
    a node type does not renumber every plan in every test.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def next(self, kind: str) -> str:
        self._counts[kind] = self._counts.get(kind, 0) + 1
        return f"{kind}_{self._counts[kind]}"


def _build(
    plan: LogicalNode,
    chosen: _Candidate,
    stats: TableStatistics,
    namer: _Namer,
) -> PhysicalNode:
    """Turn the rewritten logical plan into physical nodes over ``chosen``."""
    match plan:
        case LogicalScan():
            return chosen.leaf

        case LogicalFilter(predicate=predicate, child=child):
            below = _build(child, chosen, stats, namer)
            residual = _residual(predicate, chosen)
            if residual is None:
                # The index condition covered the whole predicate, so there is
                # nothing left to filter. This is the case Milestone 5 could not
                # express: it always kept a Filter above the index scan.
                return below
            selectivity = estimate_selectivity(residual, stats)
            return PhysicalFilter(
                node_id=namer.next("filter"),
                estimated=filter_cost(below.estimated.rows, selectivity=selectivity),
                predicate=residual,
                child=below,
            )

        case LogicalProject(projections=projections, child=child):
            below = _build(child, chosen, stats, namer)
            return PhysicalProject(
                node_id=namer.next("project"),
                estimated=project_cost(
                    below.estimated.rows, expressions=len(projections)
                ),
                projections=projections,
                output_columns=plan.output_columns,
                child=below,
            )

    raise TypeError(f"cannot plan {plan.node_type}")  # pragma: no cover


def _residual(predicate: Expression, chosen: _Candidate) -> Expression | None:
    """What the chosen leaf did not absorb, and must still be filtered."""
    if not chosen.absorbed:
        return predicate
    conjuncts = _split_conjunction(predicate)
    return _rebuild_conjunction(
        [
            conjunct
            for position, conjunct in enumerate(conjuncts)
            if position not in chosen.absorbed
        ]
    )


def _find_scan(plan: LogicalNode) -> LogicalScan:
    for node in (plan, *_descendants(plan)):
        if isinstance(node, LogicalScan):
            return node
    raise ValueError("a logical plan must contain a scan")  # pragma: no cover


def _predicate_of(plan: LogicalNode) -> Expression | None:
    for node in (plan, *_descendants(plan)):
        if isinstance(node, LogicalFilter):
            return node.predicate
    return None


def _descendants(plan: LogicalNode) -> list[LogicalNode]:
    out: list[LogicalNode] = []
    for child in plan.children:
        out.append(child)
        out.extend(_descendants(child))
    return out


# --------------------------------------------------------------------------
# Turning comparisons into index bounds
# --------------------------------------------------------------------------


def _split_conjunction(expression: Expression) -> list[Expression]:
    """Flatten ``a AND b AND c`` into ``[a, b, c]``. Anything else is one term."""
    if isinstance(expression, BinaryOp) and expression.operator is BinaryOperator.AND:
        return _split_conjunction(expression.left) + _split_conjunction(expression.right)
    return [expression]


def _rebuild_conjunction(terms: list[Expression]) -> Expression | None:
    if not terms:
        return None
    combined = terms[0]
    for term in terms[1:]:
        combined = BinaryOp(
            node_id=term.node_id,
            span=combined.span.union(term.span),
            operator=BinaryOperator.AND,
            left=combined,
            right=term,
        )
    return combined


def _as_column_comparison(
    expression: Expression,
) -> tuple[BoundColumnRef, BinaryOperator, Any] | None:
    """Match ``column <op> literal``, mirroring a reversed comparison.

    ``<>`` is refused: an index cannot bound it, so the scan would read the
    whole tree and then do a random heap read per row — strictly worse than a
    sequential scan, every time.
    """
    if not isinstance(expression, BinaryOp) or not expression.operator.is_comparison:
        return None
    if expression.operator is BinaryOperator.NEQ:
        return None

    left, right = expression.left, expression.right
    if isinstance(left, BoundColumnRef) and isinstance(right, Literal):
        return left, expression.operator, right.value
    if isinstance(right, BoundColumnRef) and isinstance(left, Literal):
        return right, _MIRRORED[expression.operator], left.value
    return None


_MIRRORED: dict[BinaryOperator, BinaryOperator] = {
    BinaryOperator.EQ: BinaryOperator.EQ,
    BinaryOperator.LT: BinaryOperator.GT,
    BinaryOperator.LTE: BinaryOperator.GTE,
    BinaryOperator.GT: BinaryOperator.LT,
    BinaryOperator.GTE: BinaryOperator.LTE,
}


def _bounds_for(
    candidates: list[tuple[int, Expression]], info: IndexInfo
) -> tuple[bytes | None, bytes | None, bool, bool, set[int]] | None:
    """Fold comparisons on one column into a single ``[low, high]`` range."""
    low: bytes | None = None
    high: bytes | None = None
    include_low = True
    include_high = True
    absorbed: set[int] = set()

    for position, comparison in candidates:
        matched = _as_column_comparison(comparison)
        if matched is None:  # pragma: no cover - the caller filtered these
            continue
        _, operator, value = matched
        if value is None:
            continue  # `x = NULL` is never true; leave it to the filter
        try:
            key = info.encode(value)
        except IndexingError:
            continue  # a literal of a type this index cannot encode

        match operator:
            case BinaryOperator.EQ:
                low = high = key
                include_low = include_high = True
            case BinaryOperator.GT | BinaryOperator.GTE:
                inclusive = operator is BinaryOperator.GTE
                if low is None or key > low or (key == low and not inclusive):
                    low, include_low = key, inclusive
            case BinaryOperator.LT | BinaryOperator.LTE:
                inclusive = operator is BinaryOperator.LTE
                if high is None or key < high or (key == high and not inclusive):
                    high, include_high = key, inclusive
            case _:  # pragma: no cover - filtered by _as_column_comparison
                continue
        absorbed.add(position)

    if not absorbed:
        return None

    # A range with no lower bound would sweep up the NULL keys, which sort below
    # every value — and no comparison is ever true for NULL. Anchoring at the
    # smallest possible *value* key excludes them.
    if low is None:
        low, include_low = SMALLEST_VALUE_KEY, True
    return low, high, include_low, include_high, absorbed


def _describe_condition(
    low: bytes | None,
    high: bytes | None,
    include_low: bool,
    include_high: bool,
    info: IndexInfo,
) -> str:
    column = info.column_name
    if low is not None and low == high and include_low and include_high:
        return f"{column} = {describe_key(low, info.data_type)}"

    parts: list[str] = []
    if low == SMALLEST_VALUE_KEY and include_low:
        parts.append(f"{column} IS NOT NULL")
    elif low is not None:
        parts.append(
            f"{column} {'>=' if include_low else '>'} {describe_key(low, info.data_type)}"
        )
    if high is not None:
        parts.append(
            f"{column} {'<=' if include_high else '<'} {describe_key(high, info.data_type)}"
        )
    return " AND ".join(parts) if parts else "full index scan"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def walk_physical(node: PhysicalNode) -> list[PhysicalNode]:
    out = [node]
    for child in node.children:
        out.extend(walk_physical(child))
    return out


def describe_physical(node: PhysicalNode, indent: int = 0) -> str:
    """Render a physical plan the way ``EXPLAIN`` prints it.

    The cost shown is **cumulative** — this node plus everything below it —
    which is what PostgreSQL prints and what a comparison between two plans
    actually uses. A per-node figure would make the root look free.
    """
    detail = f"  {node.detail}" if node.detail else ""
    lines = [
        f"{'  ' * indent}{'└─ ' if indent else ''}{node.node_type}{detail}"
        f"  (cost={node.total_cost:.1f} rows={node.estimated.rows:.0f})"
    ]
    for child in node.children:
        lines.append(describe_physical(child, indent + 1))
    return "\n".join(lines)
