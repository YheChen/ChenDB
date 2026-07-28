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
from itertools import combinations
from typing import TYPE_CHECKING, Any, Final

from engine.errors import IndexingError
from engine.executor.binder import (
    BoundAggregate,
    BoundColumnRef,
    BoundSortKey,
    ResultColumn,
    RowLayout,
)
from engine.executor.expression import describe_expression
from engine.index.key import SMALLEST_VALUE_KEY, describe_key
from engine.optimizer.cost import (
    Cost,
    aggregate_cost,
    estimate_selectivity,
    filter_cost,
    hash_join_cost,
    index_scan_cost,
    join_selectivity,
    limit_cost,
    nested_loop_join_cost,
    project_cost,
    seq_scan_cost,
    sort_cost,
)
from engine.optimizer.rules import apply_rules
from engine.parser.ast import (
    BinaryOp,
    BinaryOperator,
    Expression,
    FunctionCall,
    IsNullTest,
    Literal,
    UnaryOp,
)
from engine.parser.tokens import SourceSpan
from engine.planner.logical import (
    LogicalAggregate,
    LogicalFilter,
    LogicalJoin,
    LogicalLimit,
    LogicalNode,
    LogicalProject,
    LogicalScan,
    LogicalSort,
)
from engine.planner.statistics import TableStatistics
from engine.serialization.schema import Schema

if TYPE_CHECKING:
    from engine.catalog.catalog import IndexInfo
    from engine.database import Database

__all__ = [
    "DISABLE_COST",
    "Alternative",
    "PhysicalAggregate",
    "PhysicalFilter",
    "PhysicalHashJoin",
    "PhysicalIndexScan",
    "PhysicalLimit",
    "PhysicalNestedLoopJoin",
    "PhysicalNode",
    "PhysicalProject",
    "PhysicalSeqScan",
    "PhysicalSort",
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

#: For the synthetic ``TRUE`` a cross product joins on. It came from no source.
_NO_SPAN: Final = SourceSpan(0, 0, 1, 1)

#: One table, so the joined row *is* the table's row and there is nothing to
#: place. A module constant rather than a default argument because a dataclass
#: field cannot call anything, and every single-table plan shares this one.
_WHOLE_ROW: Final = RowLayout(0, 0, 0)


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
    layout: RowLayout = _WHOLE_ROW

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
    layout: RowLayout = _WHOLE_ROW
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


@dataclass(frozen=True, slots=True)
class PhysicalNestedLoopJoin(PhysicalNode):
    """For every outer row, scan the inner side. The algorithm of last resort.

    Kept because it is the only one that works on *any* predicate. A hash join
    needs an equality to hash; ``a.x < b.y`` has no key, so this is what is left
    — which is why a range join is slow in every engine and not just this one.
    """

    predicate: Expression
    left: PhysicalNode
    right: PhysicalNode
    right_slices: tuple[tuple[int, int], ...] = ()
    """``(offset, width)`` per table on the right, copied into the left's row."""

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.left, self.right)

    @property
    def detail(self) -> str:
        return describe_expression(self.predicate)


@dataclass(frozen=True, slots=True)
class PhysicalHashJoin(PhysicalNode):
    """Build a hash table on the left, probe it with the right.

    ``left`` is always the build side, and the planner puts the *smaller*
    estimate there — memory is proportional to it, and getting that backwards
    is the difference between a hash table of ten rows and one of ten million.
    """

    predicate: Expression
    build_key: Expression
    probe_key: Expression
    residual: Expression | None
    """The rest of the join condition, re-checked after the hash match. Hashing
    handles one equality; ``a.x = b.y AND a.z < b.w`` needs the second term
    evaluated per matching pair."""
    left: PhysicalNode
    right: PhysicalNode
    right_slices: tuple[tuple[int, int], ...] = ()
    """``(offset, width)`` per table on the right, copied into the left's row."""

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.left, self.right)

    @property
    def detail(self) -> str:
        core = (
            f"{describe_expression(self.build_key)} = {describe_expression(self.probe_key)}"
        )
        return core + (
            f" AND {describe_expression(self.residual)}" if self.residual else ""
        )


@dataclass(frozen=True, slots=True)
class PhysicalAggregate(PhysicalNode):
    """Hash each row to its group, then fold it in."""

    group_keys: tuple[Expression, ...]
    aggregates: tuple[BoundAggregate, ...]
    having: Expression | None
    child: PhysicalNode

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.child,)

    @property
    def detail(self) -> str:
        keys = ", ".join(describe_expression(key) for key in self.group_keys) or "all rows"
        functions = ", ".join(entry.label for entry in self.aggregates)
        return f"by {keys}" + (f" -> {functions}" if functions else "")


@dataclass(frozen=True, slots=True)
class PhysicalSort(PhysicalNode):
    """Buffer everything, sort it, then emit. The one blocking operator."""

    keys: tuple[BoundSortKey, ...]
    child: PhysicalNode

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.child,)

    @property
    def detail(self) -> str:
        return ", ".join(
            f"#{key.output_index}{' DESC' if key.descending else ''}" for key in self.keys
        )


@dataclass(frozen=True, slots=True)
class PhysicalLimit(PhysicalNode):
    """Stop pulling after enough rows."""

    count: int
    offset: int
    child: PhysicalNode

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.child,)

    @property
    def detail(self) -> str:
        return f"{self.count}" + (f" offset {self.offset}" if self.offset else "")


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
    decision: str = "access path"
    """Which question this was an answer to.

    With one table there was one decision and the field would have been noise.
    With joins there are several independent ones — how to read each table, and
    what order to join them in — and a flat list of winners and losers reads as
    a contradiction: three entries marked "chosen" for what looks like one
    choice. Grouping by this is what makes the plan legible again."""


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

    With one table "enumerate" meant listing access paths and taking the
    minimum. With several it means that *and* choosing an order to join them
    in, which is the first decision in this project with a search space bigger
    than a handful — see :func:`_plan_joins`.
    """
    rewritten = apply_rules(logical)
    scans = _find_scans(rewritten.plan)
    stats_by_table = {
        scan.table_name: database.statistics.for_table(scan.table_name) for scan in scans
    }
    stale = any(database.statistics.is_stale(scan.table_name) for scan in scans)

    # WHERE and every ON, in one pool. For an inner join the two are
    # interchangeable — `a JOIN b ON p` and `a, b WHERE p` mean the same thing —
    # and merging them is what lets a condition written as an ON end up pushed
    # down to a scan, or a condition written in the WHERE become a join key.
    predicate = _predicate_of(rewritten.plan)
    conjuncts = _split_conjunction(predicate) if predicate is not None else []
    for join in _find_joins(rewritten.plan):
        conjuncts.extend(_split_conjunction(join.predicate))

    namer = _Namer()
    joined, alternatives, handled = _plan_joins(
        scans, conjuncts, database, stats_by_table, options, namer
    )

    residual = _rebuild_conjunction(
        [term for position, term in enumerate(conjuncts) if position not in handled]
    )
    root = _stack(rewritten.plan, joined, residual, stats_by_table, namer)

    return PlannedQuery(
        root=root,
        logical=rewritten.plan,
        rewrites=rewritten.applied,
        alternatives=tuple(alternatives),
        statistics=stats_by_table[scans[0].table_name],
        statistics_are_stale=stale,
    )


# --------------------------------------------------------------------------
# Joins: choosing an order, then an algorithm
# --------------------------------------------------------------------------


#: Above this many tables, stop enumerating every order and take the greedy
#: one. PostgreSQL's ``geqo_threshold`` is 12 and it switches to a genetic
#: algorithm; ChenDB switches to "join the cheapest pair you can see", which is
#: worse and finishes. The number is far lower because the DP below is written
#: for clarity rather than speed — 8 tables is 6,561 subsets, which is fine, and
#: 12 would be 531,441, which is not.
MAX_TABLES_TO_ENUMERATE: Final = 8


@dataclass(frozen=True, slots=True)
class _Relation:
    """A subset of the tables, joined, with the best plan found for it."""

    tables: frozenset[int]
    node: PhysicalNode
    handled: frozenset[int]
    """Conjunct positions already applied inside this subplan."""

    @property
    def rows(self) -> float:
        return self.node.estimated.rows

    @property
    def cost(self) -> float:
        return self.node.total_cost


def _plan_joins(
    scans: list[LogicalScan],
    conjuncts: list[Expression],
    database: Database,
    stats_by_table: dict[str, TableStatistics],
    options: PlannerOptions,
    namer: _Namer,
) -> tuple[PhysicalNode, list[Alternative], frozenset[int]]:
    """Pick an access path per table, then an order to join them in.

    Returns the joined subplan, everything considered on the way, and which
    conjuncts were consumed.

    Predicate pushdown happens first and is not a choice: a conjunct that names
    one table only is applied at that table's scan, where it shrinks the input
    to every join above it. Pushing it down can never be worse, which is why it
    is a *rewrite* rather than a costed alternative — the classic example of the
    difference between the two.
    """
    alternatives: list[Alternative] = []
    relations: list[_Relation] = []
    handled: set[int] = set()

    for position, scan in enumerate(scans):
        mine = [
            index for index, term in enumerate(conjuncts) if _tables_of(term) == {position}
        ]
        chosen, considered = _choose_access_path(
            scan,
            [(index, conjuncts[index]) for index in mine],
            database,
            stats_by_table[scan.table_name],
            options,
            layout=RowLayout(scan.offset, len(scan.schema), scan.total_width),
        )
        alternatives.extend(considered)
        handled |= chosen.absorbed
        node = chosen.leaf

        # Whatever the index could not absorb still has to be filtered, and
        # doing it here rather than above the join is the pushdown.
        leftover = [index for index in mine if index not in chosen.absorbed]
        if leftover:
            below = _rebuild_conjunction([conjuncts[index] for index in leftover])
            assert below is not None
            selectivity = estimate_selectivity(below, stats_by_table[scan.table_name])
            node = PhysicalFilter(
                node_id=namer.next("filter"),
                estimated=filter_cost(node.estimated.rows, selectivity=selectivity),
                predicate=below,
                child=node,
            )
            handled.update(leftover)

        relations.append(_Relation(frozenset({position}), node, frozenset()))

    if len(relations) == 1:
        return relations[0].node, alternatives, frozenset(handled)

    joinable = [
        index
        for index, term in enumerate(conjuncts)
        if index not in handled and len(_tables_of(term)) > 1
    ]
    best = _search_join_order(
        relations, conjuncts, joinable, scans, stats_by_table, namer, alternatives
    )
    return best.node, alternatives, frozenset(handled) | best.handled


def _search_join_order(
    relations: list[_Relation],
    conjuncts: list[Expression],
    joinable: list[int],
    scans: list[LogicalScan],
    stats_by_table: dict[str, TableStatistics],
    namer: _Namer,
    alternatives: list[Alternative],
) -> _Relation:
    """System R's dynamic programme, over left-deep trees.

    Build the best plan for every one-table set, then every two-table set from
    those, and so on. The insight, and it is the whole of the algorithm: the
    best plan for ``{a, b, c}`` uses the best plan for one of its subsets, so
    each subset is solved once and reused rather than re-derived down every
    branch that contains it.

    ``n`` tables have ``2ⁿ`` subsets and the loop below is ``O(3ⁿ)`` — 27 for
    three tables, 6,561 for eight, 531,441 for twelve. That growth is why
    PostgreSQL gives up at ``geqo_threshold`` and why
    :data:`MAX_TABLES_TO_ENUMERATE` exists.

    **Left-deep only.** Bushy plans — joining ``(a⨝b)`` to ``(c⨝d)`` — are
    sometimes better and multiply the search space again. System R excluded
    them in 1979 for that reason and most optimizers still do.
    """
    if len(relations) > MAX_TABLES_TO_ENUMERATE:
        return _greedy_join_order(
            relations, conjuncts, joinable, scans, stats_by_table, namer, alternatives
        )

    best: dict[frozenset[int], _Relation] = {
        relation.tables: relation for relation in relations
    }
    everything = frozenset(range(len(relations)))

    for size in range(2, len(relations) + 1):
        for subset in _subsets_of_size(everything, size):
            for single in subset:
                rest = subset - {single}
                left = best.get(rest)
                right = best.get(frozenset({single}))
                if left is None or right is None:
                    continue
                candidate = _join(
                    left, right, conjuncts, joinable, scans, stats_by_table, namer
                )
                incumbent = best.get(subset)
                if incumbent is None or candidate.cost < incumbent.cost:
                    best[subset] = candidate

    winner = best[everything]
    _record_join_alternatives(best, everything, relations, winner, alternatives)
    return winner


def _greedy_join_order(
    relations: list[_Relation],
    conjuncts: list[Expression],
    joinable: list[int],
    scans: list[LogicalScan],
    stats_by_table: dict[str, TableStatistics],
    namer: _Namer,
    alternatives: list[Alternative],
) -> _Relation:
    """Repeatedly join the cheapest available pair. Used past the threshold.

    No guarantee of optimality, and it is reported as such rather than passed
    off as the answer — a planner that silently degrades is worse than one that
    says it gave up.
    """
    remaining = list(relations)
    while len(remaining) > 1:
        pairs = [
            (
                _join(left, right, conjuncts, joinable, scans, stats_by_table, namer),
                index,
                other,
            )
            for index, left in enumerate(remaining)
            for other, right in enumerate(remaining)
            if index != other
        ]
        joined, index, other = min(pairs, key=lambda item: item[0].cost)
        remaining = [
            relation
            for position, relation in enumerate(remaining)
            if position not in (index, other)
        ] + [joined]

    alternatives.append(
        Alternative(
            description=(
                f"greedy over {len(relations)} tables, above the "
                f"{MAX_TABLES_TO_ENUMERATE}-table enumeration limit — "
                f"this may not be the cheapest order"
            ),
            access_path="GreedyJoinOrder",
            cost=remaining[0].node.estimated,
            chosen=True,
            decision="what order to join in",
        )
    )
    return remaining[0]


def _join(
    left: _Relation,
    right: _Relation,
    conjuncts: list[Expression],
    joinable: list[int],
    scans: list[LogicalScan],
    stats_by_table: dict[str, TableStatistics],
    namer: _Namer,
) -> _Relation:
    """The cheaper of a hash join and a nested loop, for one pair of subsets."""
    tables = left.tables | right.tables
    applicable = [
        index
        for index in joinable
        if index not in left.handled
        and index not in right.handled
        and _tables_of(conjuncts[index]) <= tables
        and not _tables_of(conjuncts[index]) <= left.tables
        and not _tables_of(conjuncts[index]) <= right.tables
    ]
    predicate = _rebuild_conjunction([conjuncts[index] for index in applicable])

    slices = tuple(
        (scans[position].offset, len(scans[position].schema))
        for position in sorted(right.tables)
    )
    matches = _join_cardinality(left, right, applicable, conjuncts, scans, stats_by_table)

    if predicate is None:
        # A cross product. Not an error — `FROM a, b` with no condition means
        # exactly this — but it is the one plan whose cost really is the product.
        node: PhysicalNode = PhysicalNestedLoopJoin(
            node_id=namer.next("nestloop"),
            estimated=nested_loop_join_cost(left.rows, right.rows, matches=matches),
            predicate=Literal(node_id=0, span=_NO_SPAN, value=True, data_type=None),
            left=left.node,
            right=right.node,
            right_slices=slices,
        )
        return _Relation(tables, node, left.handled | right.handled | frozenset(applicable))

    keys = _equijoin_keys(applicable, conjuncts, left.tables)
    nested = PhysicalNestedLoopJoin(
        node_id=namer.next("nestloop"),
        estimated=nested_loop_join_cost(left.rows, right.rows, matches=matches),
        predicate=predicate,
        left=left.node,
        right=right.node,
        right_slices=slices,
    )
    best: PhysicalNode = nested

    if keys is not None:
        build_key, probe_key, used = keys
        residual = _rebuild_conjunction(
            [conjuncts[index] for index in applicable if index != used]
        )
        hashed = PhysicalHashJoin(
            node_id=namer.next("hashjoin"),
            estimated=hash_join_cost(left.rows, right.rows, matches=matches),
            predicate=predicate,
            build_key=build_key,
            probe_key=probe_key,
            residual=residual,
            left=left.node,
            right=right.node,
            right_slices=slices,
        )
        if hashed.total_cost < nested.total_cost:
            best = hashed

    return _Relation(tables, best, left.handled | right.handled | frozenset(applicable))


def _equijoin_keys(
    applicable: list[int], conjuncts: list[Expression], left_tables: frozenset[int]
) -> tuple[Expression, Expression, int] | None:
    """The first ``left.x = right.y`` among the applicable conjuncts.

    One key, not several. A composite hash key is a real optimisation and a
    real escaping problem — the same one :mod:`engine.index.key` describes —
    and the second equality is re-checked per matching pair instead.
    """
    for index in applicable:
        term = conjuncts[index]
        if not isinstance(term, BinaryOp) or term.operator is not BinaryOperator.EQ:
            continue
        left_side, right_side = term.left, term.right
        if not _tables_of(left_side) or not _tables_of(right_side):
            continue
        if (
            _tables_of(left_side) <= left_tables
            and not _tables_of(right_side) & left_tables
        ):
            return left_side, right_side, index
        if (
            _tables_of(right_side) <= left_tables
            and not _tables_of(left_side) & left_tables
        ):
            return right_side, left_side, index
    return None


def _join_cardinality(
    left: _Relation,
    right: _Relation,
    applicable: list[int],
    conjuncts: list[Expression],
    scans: list[LogicalScan],
    stats_by_table: dict[str, TableStatistics],
) -> float:
    """How many rows the join is expected to produce.

    The estimate that matters most and is trusted least: it feeds every join
    above this one, so an error here compounds up the tree rather than staying
    put. Two tables joined 10x too high makes a three-table plan 10x wrong and a
    four-table plan 100x.
    """
    product = max(left.rows, 1.0) * max(right.rows, 1.0)
    if not applicable:
        return product

    selectivity = 1.0
    for index in applicable:
        term = conjuncts[index]
        equality = isinstance(term, BinaryOp) and term.operator is BinaryOperator.EQ
        involved = sorted(_tables_of(term))
        left_stats = stats_by_table[scans[involved[0]].table_name]
        right_stats = stats_by_table[scans[involved[-1]].table_name]
        selectivity *= join_selectivity(left_stats, right_stats, equality=equality)
    return max(product * selectivity, 1.0)


def _record_join_alternatives(
    best: dict[frozenset[int], _Relation],
    everything: frozenset[int],
    relations: list[_Relation],
    winner: _Relation,
    alternatives: list[Alternative],
) -> None:
    """Report the winning order, and the two-table pairings it was built from.

    Not every subset — a four-table query has fifteen and listing them all
    would bury the answer. The pairs are the interesting ones, because that is
    where "join the small side first" is visible.
    """
    alternatives.append(
        Alternative(
            description=f"{_order_of(winner.node)}",
            access_path=winner.node.node_type,
            cost=winner.node.estimated,
            chosen=True,
            decision="what order to join in",
        )
    )
    if len(relations) < 3:
        return
    pairs = sorted(
        (
            relation
            for subset, relation in best.items()
            if len(subset) == 2 and subset != everything
        ),
        key=lambda relation: relation.cost,
    )
    for relation in pairs[:4]:
        alternatives.append(
            Alternative(
                description=_order_of(relation.node),
                access_path=relation.node.node_type,
                cost=relation.node.estimated,
                chosen=False,
                rejected_because="a building block, not a rejected plan",
                decision="what order to join in",
            )
        )


def _order_of(node: PhysicalNode) -> str:
    """``users ⨝ orders ⨝ items`` — the order the plan actually joins in."""
    if isinstance(node, PhysicalSeqScan | PhysicalIndexScan):
        return node.table_name
    if isinstance(node, PhysicalNestedLoopJoin | PhysicalHashJoin):
        return f"{_order_of(node.left)} x {_order_of(node.right)}"
    if node.children:
        return _order_of(node.children[0])
    return "?"  # pragma: no cover


def _subsets_of_size(items: frozenset[int], size: int) -> list[frozenset[int]]:
    return [frozenset(combination) for combination in combinations(sorted(items), size)]


def _tables_of(expression: Expression) -> set[int]:
    """Which tables an expression reads, by position in the ``FROM``.

    Read off the *scan position* stashed on each bound column by the logical
    planner, so this needs no scope and no lookup — and so a predicate can be
    classified as single-table or not in one walk.
    """
    return {
        node.scan_position
        for node in _walk_expression(expression)
        if isinstance(node, BoundColumnRef) and node.scan_position is not None
    }


def _walk_expression(expression: Expression) -> list[Expression]:
    out = [expression]
    match expression:
        case UnaryOp():
            out.extend(_walk_expression(expression.operand))
        case BinaryOp():
            out.extend(_walk_expression(expression.left))
            out.extend(_walk_expression(expression.right))
        case IsNullTest():
            out.extend(_walk_expression(expression.operand))
        case FunctionCall() if expression.argument is not None:
            out.extend(_walk_expression(expression.argument))
    return out


# --------------------------------------------------------------------------
# Everything above the joins
# --------------------------------------------------------------------------


def _stack(
    plan: LogicalNode,
    joined: PhysicalNode,
    residual: Expression | None,
    stats_by_table: dict[str, TableStatistics],
    namer: _Namer,
) -> PhysicalNode:
    """Put filter, aggregate, project, sort and limit back on top, in order.

    The order is SQL's evaluation order and not its written order, which is the
    single most useful thing a plan tree teaches: ``WHERE`` runs before
    ``GROUP BY``, ``HAVING`` after it, ``ORDER BY`` after the select list — so
    ``ORDER BY`` can use an alias and ``WHERE`` cannot.
    """
    stats = next(iter(stats_by_table.values()))
    node = joined

    if residual is not None:
        selectivity = estimate_selectivity(residual, stats)
        node = PhysicalFilter(
            node_id=namer.next("filter"),
            estimated=filter_cost(node.estimated.rows, selectivity=selectivity),
            predicate=residual,
            child=node,
        )

    for logical in reversed(_spine(plan)):
        match logical:
            case LogicalAggregate():
                groups = _estimate_groups(logical, node.estimated.rows, stats_by_table)
                node = PhysicalAggregate(
                    node_id=namer.next("aggregate"),
                    estimated=aggregate_cost(
                        node.estimated.rows,
                        groups=groups,
                        aggregates=len(logical.aggregates),
                    ),
                    group_keys=logical.group_keys,
                    aggregates=logical.aggregates,
                    having=logical.having,
                    child=node,
                )
            case LogicalProject():
                node = PhysicalProject(
                    node_id=namer.next("project"),
                    estimated=project_cost(
                        node.estimated.rows, expressions=len(logical.projections)
                    ),
                    projections=logical.projections,
                    output_columns=logical.output_columns,
                    child=node,
                )
            case LogicalSort():
                node = PhysicalSort(
                    node_id=namer.next("sort"),
                    estimated=sort_cost(node.estimated.rows, keys=len(logical.keys)),
                    keys=logical.keys,
                    child=node,
                )
            case LogicalLimit():
                node = PhysicalLimit(
                    node_id=namer.next("limit"),
                    estimated=limit_cost(
                        node.estimated.rows, count=logical.count, offset=logical.offset
                    ),
                    count=logical.count,
                    offset=logical.offset,
                    child=node,
                )
    return node


def _spine(plan: LogicalNode) -> list[LogicalNode]:
    """The single-child nodes above the joins, root first."""
    out: list[LogicalNode] = []
    node = plan
    while not isinstance(node, LogicalScan | LogicalJoin):
        if isinstance(node, LogicalAggregate | LogicalProject | LogicalSort | LogicalLimit):
            out.append(node)
        if not node.children:
            break  # pragma: no cover
        node = node.children[0]
    return out


def _estimate_groups(
    aggregate: LogicalAggregate,
    input_rows: float,
    stats_by_table: dict[str, TableStatistics],
) -> float:
    """How many groups a ``GROUP BY`` will produce.

    One, if there are no keys — the scalar case, and it is one group even over
    no rows. Otherwise the distinct count of the key columns if it is known,
    and ten percent of the input if it is not. That fallback is crude and
    visible: an aggregate whose estimated rows are exactly a tenth of its
    child's is an aggregate the planner is guessing about.
    """
    if not aggregate.group_keys:
        return 1.0
    known: list[float] = []
    for key in aggregate.group_keys:
        if not isinstance(key, BoundColumnRef) or key.table_name is None:
            continue
        stats = stats_by_table.get(key.table_name)
        column = stats.column(key.table_position) if stats else None
        if column is not None and column.distinct_count:
            known.append(float(column.distinct_count))
    if known:
        product = 1.0
        for value in known:
            product *= value
        return min(product, max(input_rows, 1.0))
    return max(input_rows * 0.1, 1.0)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A leaf, plus what it absorbed from the predicate."""

    leaf: PhysicalNode
    description: str
    absorbed: frozenset[int]
    """Indices into the conjunct list this leaf handles itself."""
    index_name: str | None = None


def _choose_access_path(
    scan: LogicalScan,
    mine: list[tuple[int, Expression]],
    database: Database,
    stats: TableStatistics,
    options: PlannerOptions,
    *,
    layout: RowLayout,
) -> tuple[_Candidate, list[Alternative]]:
    """Pick how to read one table, and report everything considered."""
    candidates = _enumerate(scan, mine, database, stats, options, layout=layout)
    chosen = min(candidates, key=lambda candidate: candidate.leaf.total_cost)
    reported = [
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
            decision=f"how to read {scan.table_name}",
        )
        for candidate in candidates
    ]
    return chosen, reported


def _enumerate(
    scan: LogicalScan,
    mine: list[tuple[int, Expression]],
    database: Database,
    stats: TableStatistics,
    options: PlannerOptions,
    *,
    layout: RowLayout,
) -> list[_Candidate]:
    """Every way to read the table. Always at least the sequential scan."""
    candidates = [
        _Candidate(
            leaf=PhysicalSeqScan(
                node_id=f"scan_{scan.position + 1}",
                estimated=_penalise(seq_scan_cost(stats), options.enable_seq_scan),
                table_name=scan.table_name,
                schema=scan.schema,
                layout=layout,
            ),
            description=f"Sequential scan of {scan.table_name}"
            + ("" if options.enable_seq_scan else " (disabled)"),
            absorbed=frozenset(),
        )
    ]
    if not mine:
        return candidates

    conjuncts = [term for _, term in mine]
    original = [index for index, _ in mine]
    positions_by_column: dict[int, list[int]] = {}
    for position, conjunct in enumerate(conjuncts):
        matched = _as_column_comparison(conjunct)
        if matched is not None:
            positions_by_column.setdefault(matched[0].table_position or 0, []).append(
                position
            )

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
                _rebuild_conjunction(
                    [conjuncts[position] for position in sorted(absorbed)]
                ),
                stats,
            )
            matching = max(stats.row_count * absorbed_selectivity, 1.0)
            tree = database.tree_for(info.name)
            condition = _describe_condition(low, high, include_low, include_high, info)
            candidates.append(
                _Candidate(
                    leaf=PhysicalIndexScan(
                        node_id=f"scan_{scan.position + 1}",
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
                        layout=layout,
                    ),
                    description=f"Index scan on {info.name} ({condition})"
                    + ("" if options.enable_index_scan else " (disabled)"),
                    # Back to the caller's numbering: `mine` is a slice of the
                    # whole conjunct list and `absorbed` indexes into the slice.
                    absorbed=frozenset(original[position] for position in absorbed),
                    index_name=info.name,
                )
            )
    return candidates


def _penalise(cost: Cost, enabled: bool) -> Cost:
    return (
        cost if enabled else Cost(io=cost.io + DISABLE_COST, cpu=cost.cpu, rows=cost.rows)
    )


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


def _find_joins(plan: LogicalNode) -> list[LogicalJoin]:
    return [node for node in (plan, *_descendants(plan)) if isinstance(node, LogicalJoin)]


def _find_scans(plan: LogicalNode) -> list[LogicalScan]:
    """Every base table, in the order the ``FROM`` named them."""
    found = [node for node in (plan, *_descendants(plan)) if isinstance(node, LogicalScan)]
    if not found:  # pragma: no cover - the binder always produces one
        raise ValueError("a logical plan must contain a scan")
    return sorted(found, key=lambda scan: scan.position)


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
