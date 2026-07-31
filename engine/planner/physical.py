"""Physical planning: enumerate the ways to run a logical plan, then cost them.

This is the module Milestone 5 was missing.  Its planner picked an index
whenever one covered a comparison, which is right below about 14% selectivity
and wrong (by 3.8x) above it.  Here the same candidates are *generated*, each
is costed against real statistics, and the cheapest wins.

    LogicalScan(users)  +  WHERE bucket < 700
            │
            ├── PhysicalSeqScan            cost 3233   ← chosen
            └── PhysicalIndexScan(bucket)  cost 11553   rejected: 3.6x the cost
                                                        of a sequential scan

Every candidate is kept, costed, and reported, not just the winner.  A planner
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
``(2n-2)! / (n-1)!`` left-deep orders (30,240 for six tables, 17 billion for
ten) which is why PostgreSQL enumerates exhaustively only below
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
from engine.index.key import SMALLEST_VALUE_KEY, decode_key, describe_key
from engine.optimizer.cost import (
    Cost,
    aggregate_cost,
    distinct_join_selectivity,
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
    JoinKind,
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
    "PhysicalJoin",
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
#: fetches, so an estimate is fine, and reading the tree to find out would make
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
        """This node plus everything below it, what a comparison uses."""
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
class PhysicalJoin(PhysicalNode):
    """What both join algorithms carry, including which side survives no match.

    ``preserve_left`` and ``preserve_right`` are about the *physical* inputs, not
    about the ``LEFT`` or ``RIGHT`` a user wrote. The two differ: the planner is
    free to put either logical side in either position (build side selection is
    the whole reason it wants to) so ``a LEFT JOIN b`` planned with ``b`` as the
    physical left is ``preserve_right=True``.

    That is what makes the flags the right representation. An outer join's inputs
    *may* be swapped, provided the flags swap with them: the output row is
    identical either way, because :class:`~engine.executor.binder.RowLayout` fixes
    every column's position by the written order of the ``FROM``. So the cost model
    keeps its freedom to choose a build side, and only the *order relative to other
    joins* is constrained.
    """

    predicate: Expression
    left: PhysicalNode
    right: PhysicalNode
    right_slices: tuple[tuple[int, int], ...] = ()
    """``(offset, width)`` per table on the right, copied into the left's row."""
    preserve_left: bool = False
    """Emit a left row with no partner, NULL-extended."""
    preserve_right: bool = False
    """Emit a right row with no partner, NULL-extended."""

    @property
    def children(self) -> tuple[PhysicalNode, ...]:
        return (self.left, self.right)

    @property
    def outer_label(self) -> str:
        """``LEFT``/``RIGHT``/``FULL`` as the *plan* runs it, or empty.

        Shown in ``EXPLAIN``, and the reason it is here rather than derived from
        the AST: an outer join whose inputs the planner swapped is genuinely a
        right join at this node, and a plan display that said ``LEFT`` because the
        query did would be describing something that is not happening.
        """
        if self.preserve_left and self.preserve_right:
            return "FULL"
        if self.preserve_left:
            return "LEFT"
        if self.preserve_right:
            return "RIGHT"
        return ""


@dataclass(frozen=True, slots=True)
class PhysicalNestedLoopJoin(PhysicalJoin):
    """For every outer row, scan the inner side. The algorithm of last resort.

        Kept because it is the only one that works on *any* predicate. A hash join
        needs an equality to hash; ``a.x < b.y`` has no key, so this is what is left
    , which is why a range join is slow in every engine and not just this one.

        It is also the only one that can do a ``FULL`` join here: see
        :class:`PhysicalHashJoin`.
    """

    @property
    def detail(self) -> str:
        label = f"{self.outer_label} " if self.outer_label else ""
        return f"{label}{describe_expression(self.predicate)}"


@dataclass(frozen=True, slots=True)
class PhysicalHashJoin(PhysicalJoin):
    """Build a hash table on the left, probe it with the right.

    ``left`` is always the build side, and the planner puts the *smaller*
    estimate there, memory is proportional to it, and getting that backwards
    is the difference between a hash table of ten rows and one of ten million.

    Both outer directions work, and neither is free. Preserving the *probe* side
    is easy: a probe row that finds no bucket is emitted on the spot. Preserving
    the *build* side needs a set of which build rows were ever matched and a pass
    over the leftovers after the probe input runs dry, which is why the operator
    keeps the build rows in insertion order rather than only in buckets.
    """

    build_key: Expression = None  # type: ignore[assignment]
    probe_key: Expression = None  # type: ignore[assignment]
    residual: Expression | None = None
    """The rest of the join condition, re-checked after the hash match. Hashing
    handles one equality; ``a.x = b.y AND a.z < b.w`` needs the second term
    evaluated per matching pair."""

    @property
    def detail(self) -> str:
        label = f"{self.outer_label} " if self.outer_label else ""
        core = (
            f"{describe_expression(self.build_key)} = {describe_expression(self.probe_key)}"
        )
        return (
            label
            + core
            + (f" AND {describe_expression(self.residual)}" if self.residual else "")
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
    A disabled path is penalised, not removed, see :data:`DISABLE_COST`.
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
    With joins there are several independent ones (how to read each table, and
    what order to join them in) and a flat list of winners and losers reads as
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
    than a handful, see :func:`_plan_joins`.
    """
    rewritten = apply_rules(logical)
    scans = _find_scans(rewritten.plan)
    stats_by_table = {
        scan.table_name: database.statistics.for_table(scan.table_name) for scan in scans
    }
    stale = any(database.statistics.is_stale(scan.table_name) for scan in scans)

    # WHERE and every *inner* ON, in one pool. For an inner join the two are
    # interchangeable (`a JOIN b ON p` and `a, b WHERE p` mean the same thing)
    # and merging them is what lets a condition written as an ON end up pushed
    # down to a scan, or a condition written in the WHERE become a join key.
    #
    # For an outer join none of that holds, and the difference is not subtle.
    # `a LEFT JOIN b ON a.id = b.id AND b.y > 5` keeps every row of `a`; the same
    # predicate in the WHERE throws away the NULL-extended ones. So an outer
    # join's ON stays *at* its join, and never enters this pool: which is also
    # why it cannot be pushed down to a scan or pulled up into a filter.
    steps = _join_steps(rewritten.plan)
    predicate = _predicate_of(rewritten.plan)
    conjuncts = _split_conjunction(predicate) if predicate is not None else []
    for step in steps:
        if step.kind is JoinKind.INNER:
            conjuncts.extend(_split_conjunction(step.on))

    namer = _Namer()
    joined, alternatives, handled = _plan_joins(
        scans, conjuncts, steps, database, stats_by_table, options, namer
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
#: for clarity rather than speed, 8 tables is 6,561 subsets, which is fine, and
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


@dataclass(frozen=True, slots=True)
class _Step:
    """One ``JOIN b ON …`` in the order it was written."""

    kind: JoinKind
    on: Expression
    position: int
    """Which scan is on the right of this join."""


def _join_steps(plan: LogicalNode) -> list[_Step]:
    """The join chain in *written* order.

    :func:`build_logical_plan` emits a left-deep chain in written order, and
    :func:`_find_joins` walks parents before children, so the outermost join
    comes back first and reversing gives the order the user typed. That order is
    what an outer join constrains, so it has to be recoverable.
    """
    steps: list[_Step] = []
    for join in reversed(_find_joins(plan)):
        right = join.right
        # Left-deep by construction: the right of every join is a single scan.
        assert isinstance(right, LogicalScan), "the join chain is not left-deep"
        steps.append(_Step(join.kind, join.predicate, right.position))
    return steps


def _null_supplied(steps: list[_Step]) -> frozenset[int]:
    """Scan positions that an outer join can NULL-extend.

    A predicate on one of these must **not** be pushed below the join. Consider
    ``a LEFT JOIN b ON a.id = b.id WHERE b.y > 5``: pushed to ``b``'s scan and
    consumed, the surviving ``a`` rows come back NULL-extended, when the WHERE
    should have rejected them. Kept above the join it is correct, and it rejects
    them for free, ``NULL > 5`` is NULL, which is not TRUE.

    Pushing to the *preserved* side stays legal and still happens, which is what
    keeps ``a LEFT JOIN b WHERE a.x = 5`` fast.

    ``RIGHT`` is why this is computed by walking rather than read off one step:
    it null-extends everything accumulated to its left, which is every table
    written before it.
    """
    nullable: set[int] = set()
    seen: list[int] = [0]
    for step in steps:
        if step.kind.preserves_left:
            nullable.add(step.position)
        if step.kind.preserves_right:
            nullable.update(seen)
        seen.append(step.position)
    return frozenset(nullable)


def _plan_joins(
    scans: list[LogicalScan],
    conjuncts: list[Expression],
    steps: list[_Step],
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
    to every join above it. Pushing it down can never be worse, *unless* the
    table can be NULL-extended by an outer join above, which is what
    :func:`_null_supplied` excludes.

    Order is then a search over the inner-join segments only. See
    :func:`_plan_chain`.
    """
    alternatives: list[Alternative] = []
    leaves: dict[int, _Relation] = {}
    handled: set[int] = set()
    protected = _null_supplied(steps)

    for position, scan in enumerate(scans):
        mine = (
            []
            if position in protected
            else [
                index
                for index, term in enumerate(conjuncts)
                if _tables_of(term) == {position}
            ]
        )
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

        leaves[position] = _Relation(frozenset({position}), node, frozenset())

    if len(leaves) == 1:
        return leaves[0].node, alternatives, frozenset(handled)

    joinable = [
        index
        for index, term in enumerate(conjuncts)
        if index not in handled and len(_tables_of(term)) > 1
    ]
    best = _plan_chain(
        leaves, steps, conjuncts, joinable, scans, stats_by_table, namer, alternatives
    )
    return best.node, alternatives, frozenset(handled) | best.handled


def _plan_chain(
    leaves: dict[int, _Relation],
    steps: list[_Step],
    conjuncts: list[Expression],
    joinable: list[int],
    scans: list[LogicalScan],
    stats_by_table: dict[str, TableStatistics],
    namer: _Namer,
    alternatives: list[Alternative],
) -> _Relation:
    """Reorder the inner joins; run the outer ones where they were written.

    **An outer join is a barrier.** Walking the chain left to right, consecutive
    inner joins accumulate into a *segment* that the System-R search may order
    however it likes. An outer join closes the segment, runs at that point with
    its own ``ON``, and the result becomes one opaque relation that the next
    segment's search sees as a single input.

    Opaque is exactly the right amount of freedom, and it falls out for free.
    The search can *commute* that relation with others. An inner join's two
    inputs may be swapped, so joining ``c`` to ``(a ⟕ b)`` either way round is
    sound. It cannot *re-associate* into it, because the relation is one item in
    the search's world and there is nothing inside to reach: it can never build
    ``a ⟕ (b ⨝ c)`` from ``(a ⟕ b) ⨝ c``, and those are genuinely different
    queries.

    **What this gives up**, stated rather than hidden: an inner join written after
    an outer one cannot move before it, even where that would be legal and
    cheaper. The general treatment is PostgreSQL's. A ``SpecialJoinInfo`` per
    outer join carrying ``min_lefthand`` and ``min_righthand`` relation sets, so
    the search can prove a particular reordering safe by set containment rather
    than assuming it is not. That is the right answer for a planner that must be
    fast on twelve-table queries. Here it would be a large amount of machinery to
    recover orderings for a shape (an outer join with inner joins after it) that
    the cost model cannot yet estimate well anyway, and the honest version of "we
    do not reorder across an outer join" is one sentence in ``EXPLAIN``.
    """
    result: _Relation | None = None
    segment: list[int] = [0]

    for step in steps:
        if step.kind is JoinKind.INNER:
            segment.append(step.position)
            continue

        left = _plan_segment(
            leaves,
            segment,
            result,
            conjuncts,
            joinable,
            scans,
            stats_by_table,
            namer,
            alternatives,
        )
        result = _join(
            left,
            leaves[step.position],
            conjuncts,
            joinable,
            scans,
            stats_by_table,
            namer,
            kind=step.kind,
            on=step.on,
        )
        alternatives.append(
            Alternative(
                description=(
                    f"{_order_of(result.node)}: an outer join runs where it was "
                    f"written, so the search may not reorder across it"
                ),
                access_path=result.node.node_type,
                cost=result.node.estimated,
                chosen=True,
                decision="what order to join in",
            )
        )
        segment = []

    return _plan_segment(
        leaves,
        segment,
        result,
        conjuncts,
        joinable,
        scans,
        stats_by_table,
        namer,
        alternatives,
    )


def _plan_segment(
    leaves: dict[int, _Relation],
    segment: list[int],
    seeded: _Relation | None,
    conjuncts: list[Expression],
    joinable: list[int],
    scans: list[LogicalScan],
    stats_by_table: dict[str, TableStatistics],
    namer: _Namer,
    alternatives: list[Alternative],
) -> _Relation:
    """One run of inner joins, ordered freely, optionally onto a fixed relation."""
    relations = ([seeded] if seeded is not None else []) + [
        leaves[position] for position in segment
    ]
    if not relations:  # pragma: no cover - a step always leaves something to join
        raise AssertionError("a join segment cannot be empty")
    if len(relations) == 1:
        return relations[0]
    return _search_join_order(
        relations, conjuncts, joinable, scans, stats_by_table, namer, alternatives
    )


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

    ``n`` tables have ``2ⁿ`` subsets and the loop below is ``O(3ⁿ)``, 27 for
    three tables, 6,561 for eight, 531,441 for twelve. That growth is why
    PostgreSQL gives up at ``geqo_threshold`` and why
    :data:`MAX_TABLES_TO_ENUMERATE` exists.

    **Left-deep only.** Bushy plans (joining ``(a⨝b)`` to ``(c⨝d)``) are
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
    off as the answer. A planner that silently degrades is worse than one that
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
                f"{MAX_TABLES_TO_ENUMERATE}-table enumeration limit, "
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
    *,
    kind: JoinKind = JoinKind.INNER,
    on: Expression | None = None,
) -> _Relation:
    """The cheaper of a hash join and a nested loop, for one pair of subsets.

    ``kind`` and ``on`` are supplied only for an outer join, whose predicate comes
    from its own ``ON`` rather than from the shared pool. That separation is the
    whole reason an outer join's condition behaves differently from a ``WHERE``.

    ``left`` is the physical left, and for an outer join it is the *preserved*
    side as written, because :func:`_plan_chain` hands the sides over in written
    order. If a later change lets the two be swapped for a cheaper build side, the
    preserve flags must swap with them, see :class:`PhysicalJoin`.
    """
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
    if kind.is_outer:
        # An outer join takes its own ON and nothing else. A pooled conjunct
        # applied here would be a WHERE evaluated *before* NULL-extension, which
        # is the one thing an outer join must not do.
        applicable = []
        predicate = on
    else:
        predicate = _rebuild_conjunction([conjuncts[index] for index in applicable])

    slices = tuple(
        (scans[position].offset, len(scans[position].schema))
        for position in sorted(right.tables)
    )
    # The join's own conditions: pooled conjuncts for an inner join, the ON's
    # own terms for an outer one, which are never in the pool.
    costed = (
        _split_conjunction(predicate)
        if kind.is_outer and predicate is not None
        else [conjuncts[index] for index in applicable]
    )
    matches = _join_cardinality(left, right, costed, scans, stats_by_table, kind=kind)
    preserving = {
        "preserve_left": kind.preserves_left,
        "preserve_right": kind.preserves_right,
    }

    if predicate is None:
        # A cross product. Not an error (`FROM a, b` with no condition means
        # exactly this) but it is the one plan whose cost really is the product.
        node: PhysicalNode = PhysicalNestedLoopJoin(
            node_id=namer.next("nestloop"),
            estimated=nested_loop_join_cost(left.rows, right.rows, matches=matches),
            predicate=Literal(node_id=0, span=_NO_SPAN, value=True, data_type=None),
            left=left.node,
            right=right.node,
            right_slices=slices,
            **preserving,
        )
        return _Relation(tables, node, left.handled | right.handled | frozenset(applicable))

    keys = (
        _equijoin_keys(applicable, conjuncts, left.tables)
        if not kind.is_outer
        else (_outer_equijoin_keys(predicate, left.tables))
    )
    nested = PhysicalNestedLoopJoin(
        node_id=namer.next("nestloop"),
        estimated=nested_loop_join_cost(left.rows, right.rows, matches=matches),
        predicate=predicate,
        left=left.node,
        right=right.node,
        right_slices=slices,
        **preserving,
    )
    best: PhysicalNode = nested

    if keys is not None:
        build_key, probe_key, used = keys
        if kind.is_outer:
            # An outer join's terms come from its own ON, which was never in the
            # pool: so the residual is the rest of *that*, not the rest of
            # `applicable` (which is empty here, and would silently drop
            # `ON a.id = b.id AND b.y > 5`'s second term).
            terms = _split_conjunction(predicate)
            residual = _rebuild_conjunction(
                [term for index, term in enumerate(terms) if index != used]
            )
        else:
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
            **preserving,
            right_slices=slices,
        )
        if hashed.total_cost < nested.total_cost:
            best = hashed

    return _Relation(tables, best, left.handled | right.handled | frozenset(applicable))


def _outer_equijoin_keys(
    predicate: Expression, left_tables: frozenset[int]
) -> tuple[Expression, Expression, int] | None:
    """The hashable equality inside an outer join's ``ON``, if there is one.

    Separate from :func:`_equijoin_keys` only because that one indexes into the
    shared conjunct pool and an outer join's terms are never in it. The returned
    position indexes the ``ON``'s own conjuncts, which is what the caller needs to
    rebuild the residual, and nothing marks an outer ``ON`` as *handled*, because
    it was never a pool entry that could be applied twice.
    """
    terms = _split_conjunction(predicate)
    return _equijoin_keys(list(range(len(terms))), terms, left_tables)


def _equijoin_keys(
    applicable: list[int], conjuncts: list[Expression], left_tables: frozenset[int]
) -> tuple[Expression, Expression, int] | None:
    """The first ``left.x = right.y`` among the applicable conjuncts.

    One key, not several. A composite hash key is a real optimisation and a
    real escaping problem (the same one :mod:`engine.index.key` describes)
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


def _joined_distinct_counts(
    term: Expression, stats_by_table: dict[str, TableStatistics]
) -> tuple[int, int] | None:
    """Distinct values in the two columns an equijoin compares, when both are known.

    The textbook estimate for ``a.x = b.y`` is ``1 / max(distinct(a.x),
    distinct(b.y))``, and :func:`distinct_join_selectivity` has spelled it that
    way since Milestone 6. Nothing called it. :func:`join_selectivity` passed
    *row counts* where distinct counts belong, and the difference is not a
    rounding error: a foreign-key join of 50 users to 4,000 orders came out at
    ``50 * 4000 / 4000``, which is 50, when the real answer is 4,000. The
    estimate was the size of the wrong side.

    It surfaced measuring Milestone 19, because the rewrite makes the estimate
    matter more: the two tables rejoin the order search, and a search cannot
    choose well between plans it cannot size. Wrong by 80x here, and it compounds
    upward, so a three-table plan is wrong by 6,400.

    ``None`` when either side is not a plain analyzed column, which falls back to
    the row-count approximation rather than inventing a number.
    """
    if not isinstance(term, BinaryOp) or term.operator is not BinaryOperator.EQ:
        return None
    counts: list[int] = []
    for side in (term.left, term.right):
        if not isinstance(side, BoundColumnRef) or side.table_position is None:
            return None
        stats = stats_by_table.get(side.table_name or "")
        column = stats.column(side.table_position) if stats is not None else None
        if column is None or column.distinct_count <= 0:
            return None
        counts.append(column.distinct_count)
    return counts[0], counts[1]


def _join_cardinality(
    left: _Relation,
    right: _Relation,
    terms: list[Expression],
    scans: list[LogicalScan],
    stats_by_table: dict[str, TableStatistics],
    *,
    kind: JoinKind = JoinKind.INNER,
) -> float:
    """How many rows the join is expected to produce.

    The estimate that matters most and is trusted least: it feeds every join
    above this one, so an error here compounds up the tree rather than staying
    put. Two tables joined 10x too high makes a three-table plan 10x wrong and a
    four-table plan 100x.

    ``terms`` are the join's own conditions, whatever they came from. It used to
    take positions into the shared conjunct pool, and an outer join has none,
    which quietly turned every outer join into a *cross product* for costing
    purposes: ``a LEFT JOIN b ON a.id = b.aid`` over 60 and 300 rows was estimated
    at 18,000 instead of 90, because the equality it joins on was not in the list
    the estimator was reading from.

    An outer join also has a **floor**, which an inner join does not: every
    preserved row appears whether it matched or not. Without it a selective
    ``LEFT JOIN`` is estimated below its own left input, which is not merely
    imprecise but impossible. PostgreSQL clamps the same way in
    ``calc_joinrel_size_estimate``.
    """
    product = max(left.rows, 1.0) * max(right.rows, 1.0)
    for term in terms:
        involved = sorted(_tables_of(term))
        if len(involved) < 2:
            continue  # single-table: a filter's selectivity, not a join's
        if (distinct := _joined_distinct_counts(term, stats_by_table)) is not None:
            product *= distinct_join_selectivity(*distinct)
            continue
        equality = isinstance(term, BinaryOp) and term.operator is BinaryOperator.EQ
        left_stats = stats_by_table[scans[involved[0]].table_name]
        right_stats = stats_by_table[scans[involved[-1]].table_name]
        product *= join_selectivity(left_stats, right_stats, equality=equality)

    floor = 1.0
    if kind.preserves_left:
        floor = max(floor, left.rows)
    if kind.preserves_right:
        floor = max(floor, right.rows)
    return max(product, floor)


def _record_join_alternatives(
    best: dict[frozenset[int], _Relation],
    everything: frozenset[int],
    relations: list[_Relation],
    winner: _Relation,
    alternatives: list[Alternative],
) -> None:
    """Report the winning order, and the two-table pairings it was built from.

    Not every subset. A four-table query has fifteen and listing them all
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
    """``users ⨝ orders ⨝ items``, the order the plan actually joins in.

    An outer join is spelled out rather than shown as ``x``, because "the order it
    joins in" is the one thing an outer join constrains and a display that hid the
    flavour would be reporting a plan that is not running.
    """
    if isinstance(node, PhysicalSeqScan | PhysicalIndexScan):
        return node.table_name
    if isinstance(node, PhysicalJoin):
        operator = f" {node.outer_label} " if node.outer_label else " x "
        return f"{_order_of(node.left)}{operator}{_order_of(node.right)}"
    if node.children:
        return _order_of(node.children[0])
    return "?"  # pragma: no cover


def _subsets_of_size(items: frozenset[int], size: int) -> list[frozenset[int]]:
    return [frozenset(combination) for combination in combinations(sorted(items), size)]


def _tables_of(expression: Expression) -> set[int]:
    """Which tables an expression reads, by position in the ``FROM``.

    Read off the *scan position* stashed on each bound column by the logical
    planner, so this needs no scope and no lookup, and so a predicate can be
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
    ``GROUP BY``, ``HAVING`` after it, ``ORDER BY`` after the select list, so
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

    One, if there are no keys. The scalar case, and it is one group even over
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
    whole tree and then do a random heap read per row, strictly worse than a
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
        if decode_key(key, info.data_type) != value:
            # The key encoding lost something, so this bound is not the
            # predicate. A FLOAT index encodes its key as a double, and
            # ``f = 9223372036854775807`` against one rounds *up* to 2⁶³: so the
            # index answered ``=`` with the row a sequential scan excludes, and
            # ``>`` by excluding the row a scan returns. Both answers inverted,
            # and only when an index happened to exist: adding an index changed
            # the result of a query, which is the one thing an index must never
            # do.
            #
            # Skipping leaves the comparison to a filter over exact values.
            # A range bound *could* be kept if it were widened in the safe
            # direction and the predicate re-checked above, which is what
            # PostgreSQL's lossy index scans do: but it must then not be
            # absorbed, and the case is a FLOAT column compared to an integer
            # too large to be a double. Correct and occasionally slower beats
            # clever here.
            continue

        match operator:
            case BinaryOperator.EQ:
                # An equality is a lower *and* an upper bound, and both have to be
                # **intersected** with what is already there rather than replace
                # it. Assigning `low = high = key` looked equivalent and was not:
                # `WHERE id = 3 AND id = 2` folded to `id = 2` and marked *both*
                # conjuncts absorbed, so `id = 3` was dropped and the query
                # returned the row with id 2. Unsatisfiable predicates are not a
                # curiosity: they are what a generated query produces constantly,
                # and what a query built by string concatenation produces by
                # accident.
                #
                # Intersecting gives low=3, high=2 for that pair: an empty range,
                # which is the right answer and one the B+ tree already handles.
                if low is None or key > low:
                    low, include_low = key, True
                if high is None or key < high:
                    high, include_high = key, True
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
    # every value: and no comparison is ever true for NULL. Anchoring at the
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

    The cost shown is **cumulative** (this node plus everything below it)
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
