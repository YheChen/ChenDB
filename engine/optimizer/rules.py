"""Rewrite rules: making a plan cheaper without changing what it means.

A rule takes a logical plan and returns a logical plan that produces **exactly
the same rows**.  That is the whole contract, and it is what separates a rewrite
from an optimisation the user has to opt into.  Rules run before costing,
because a rule that removes work removes it from every candidate at once.

    Project(a, b)                       Project(a, b)
      └─ Filter(TRUE)         ─────▶      └─ Filter(age >= 18)
           └─ Filter(age >= 18)                └─ Scan(users)
                └─ Scan(users)

Each rule reports whether it fired, so ``EXPLAIN`` and the plan view can show
which rewrites applied. A plan that is mysteriously fast is only useful if you
can see why.

What is here, and what is not
-----------------------------
Constant folding runs the arithmetic once instead of once per row, two
eliminations remove whole operators from the pipeline, and one rule changes what
a join *is*. Milestone 3 already did the identity-projection one inline; moving
it here is the point of having a rules module. It becomes inspectable and
testable in isolation rather than being a conditional buried in plan
construction.

The two rewrites a textbook names first are elsewhere on purpose, because both
depend on costs and on which table an expression reads:

* **Predicate pushdown** is in :func:`~engine.planner.physical._plan_joins`,
  where a conjunct that names one table is applied at that table's scan.
* **Join reordering** is :func:`~engine.planner.physical._search_join_order`,
  System R's dynamic programme, and it needs the cost model in hand.

:func:`_simplify_outer_joins` is the rule that makes both of those work harder.
Neither may cross an outer join, so proving that an outer join is really an inner
one hands two tables back to the search and unblocks pushdown into a side that
was protected a moment earlier. It is the only rule here that pays off through
other parts of the planner rather than by itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from engine.errors import EvaluationError
from engine.executor.binder import BoundColumnRef
from engine.executor.expression import evaluate
from engine.optimizer.nullability import rejects_nulls
from engine.parser.ast import (
    BinaryOp,
    Expression,
    IsNullTest,
    JoinKind,
    Literal,
    UnaryOp,
)
from engine.planner.logical import (
    LogicalFilter,
    LogicalJoin,
    LogicalNode,
    LogicalProject,
    LogicalScan,
)
from engine.serialization.types import DataType

__all__ = ["RULES", "RewriteResult", "Rule", "apply_rules"]


@dataclass(frozen=True, slots=True)
class Rule:
    """One rewrite, with the name the plan view shows."""

    name: str
    description: str
    apply: Callable[[LogicalNode], LogicalNode]


@dataclass(frozen=True, slots=True)
class RewriteResult:
    """The rewritten plan, and which rules actually changed it."""

    plan: LogicalNode
    applied: tuple[str, ...]


def apply_rules(plan: LogicalNode, rules: tuple[Rule, ...] | None = None) -> RewriteResult:
    """Run every rule once, in order, keeping only the ones that changed anything.

    One pass, not to a fixed point. A fixed-point loop is what a real optimiser
    does (a rule can expose an opportunity for an earlier one) but it needs a
    termination argument, and with four rules that never re-enable each other
    the loop would be honesty theatre. Add it when a rule pair needs it.
    """
    applied: list[str] = []
    for rule in rules if rules is not None else RULES:
        rewritten = rule.apply(plan)
        if rewritten is not plan:
            applied.append(rule.name)
            plan = rewritten
    return RewriteResult(plan=plan, applied=tuple(applied))


# --------------------------------------------------------------------------
# Expression rewriting
# --------------------------------------------------------------------------


def fold_constants_in(expression: Expression) -> Expression:
    """Evaluate any subtree that does not depend on a row.

    ``age > 2 * 5`` becomes ``age > 10``: the multiplication happens once at
    plan time instead of once per row. On a million-row scan that is a million
    saved evaluations, and it also makes the predicate matchable by the index
    planner, which only recognises ``column <op> literal``.

    Folding is skipped when evaluation raises. ``1 / 0`` must fail when the
    query *runs*, not when it is planned. A plan that cannot be produced is a
    worse error than a query that fails, and a folded division by zero in a
    branch that never executes would fail a query that should have succeeded.
    """
    match expression:
        case Literal() | BoundColumnRef():
            return expression

        case UnaryOp(operand=operand):
            # Rebuild only when a child actually changed. Returning a fresh but
            # identical node would make apply_rules() report the rule as having
            # fired on every query, which turns "rewrites applied" from
            # information into noise.
            inner = fold_constants_in(operand)
            rebuilt = (
                expression
                if inner is operand
                else UnaryOp(
                    node_id=expression.node_id,
                    span=expression.span,
                    operator=expression.operator,
                    operand=inner,
                )
            )
            return _try_fold(rebuilt)

        case BinaryOp(left=left, right=right):
            new_left, new_right = fold_constants_in(left), fold_constants_in(right)
            rebuilt = (
                expression
                if new_left is left and new_right is right
                else BinaryOp(
                    node_id=expression.node_id,
                    span=expression.span,
                    operator=expression.operator,
                    left=new_left,
                    right=new_right,
                )
            )
            return _try_fold(rebuilt)

        case IsNullTest(operand=operand):
            inner = fold_constants_in(operand)
            if inner is operand:
                return expression
            return IsNullTest(
                node_id=expression.node_id,
                span=expression.span,
                operand=inner,
                negated=expression.negated,
            )

    return expression


def _try_fold(expression: Expression) -> Expression:
    if isinstance(expression, Literal) or not _is_constant(expression):
        return expression
    try:
        value = evaluate(expression, ())
    except (EvaluationError, ZeroDivisionError):
        return expression
    return Literal(
        node_id=expression.node_id,
        span=expression.span,
        value=value,
        data_type=_type_of(value),
    )


def _is_constant(expression: Expression) -> bool:
    """Whether an expression can be evaluated with no row."""
    match expression:
        case Literal():
            return True
        case BoundColumnRef():
            return False
        case UnaryOp(operand=operand):
            return _is_constant(operand)
        case BinaryOp(left=left, right=right):
            return _is_constant(left) and _is_constant(right)
        case IsNullTest(operand=operand):
            return _is_constant(operand)
    return False


def _type_of(value: object) -> DataType | None:
    match value:
        case bool():
            return DataType.BOOLEAN
        case int():
            return DataType.INTEGER
        case float():
            return DataType.FLOAT
        case str():
            return DataType.TEXT
    return None


# --------------------------------------------------------------------------
# Plan rewriting
# --------------------------------------------------------------------------


def _fold_constants(plan: LogicalNode) -> LogicalNode:
    match plan:
        case LogicalFilter(predicate=predicate, child=child):
            folded = fold_constants_in(predicate)
            rewritten_child = _fold_constants(child)
            if folded is predicate and rewritten_child is child:
                return plan
            return LogicalFilter(plan.node_id, folded, rewritten_child)

        case LogicalProject(projections=projections, child=child):
            folded = tuple(fold_constants_in(item) for item in projections)
            rewritten_child = _fold_constants(child)
            if all(a is b for a, b in zip(folded, projections, strict=True)) and (
                rewritten_child is child
            ):
                return plan
            return LogicalProject(
                plan.node_id, folded, plan.output_columns, rewritten_child
            )

    return plan


def _remove_trivial_filter(plan: LogicalNode) -> LogicalNode:
    """Drop ``WHERE TRUE``, which constant folding is what produces.

    ``WHERE FALSE`` is deliberately *not* turned into an empty scan. It could
    be (that is a real rule real planners have) but it needs a physical
    operator that produces nothing, and inventing one for a query nobody writes
    would be structure without a user.
    """
    match plan:
        case LogicalFilter(predicate=Literal(value=True), child=child):
            return _remove_trivial_filter(child)
        case LogicalFilter(predicate=predicate, child=child):
            rewritten = _remove_trivial_filter(child)
            return (
                plan
                if rewritten is child
                else LogicalFilter(plan.node_id, predicate, rewritten)
            )
        case LogicalProject(child=child):
            rewritten = _remove_trivial_filter(child)
            return (
                plan
                if rewritten is child
                else LogicalProject(
                    plan.node_id, plan.projections, plan.output_columns, rewritten
                )
            )
    return plan


def _drop_identity_projection(plan: LogicalNode) -> LogicalNode:
    """Skip a projection that copies every column, in order, unchanged.

    A real saving: one method call and one tuple build per row. Milestone 3 did
    this inline in plan construction; here it is a rule that can be named in
    ``EXPLAIN`` and tested on its own.
    """
    if not isinstance(plan, LogicalProject):
        return plan
    schema = _scanned_schema(plan)
    if schema is None or len(plan.projections) != len(schema):
        return plan
    identity = all(
        isinstance(projection, BoundColumnRef) and projection.column_index == position
        for position, projection in enumerate(plan.projections)
    )
    return plan.child if identity else plan


def _merge_adjacent_filters(plan: LogicalNode) -> LogicalNode:
    """Combine ``Filter(a)`` over ``Filter(b)`` into ``Filter(a AND b)``.

    Nothing produces stacked filters today (the binder emits at most one) so
    this is the rule that fires least. It stays because it is the shape
    predicate pushdown produces the moment there is a join to push through, and
    because it is one place the "same rows out" contract is easy to see: AND is
    exactly what two filters in series compute.
    """
    match plan:
        case LogicalFilter(
            predicate=outer, child=LogicalFilter(predicate=inner, child=grandchild)
        ):
            from engine.parser.ast import BinaryOperator

            combined = BinaryOp(
                node_id=plan.node_id,
                span=outer.span.union(inner.span),
                operator=BinaryOperator.AND,
                left=outer,
                right=inner,
            )
            return _merge_adjacent_filters(
                LogicalFilter(plan.node_id, combined, grandchild)
            )
        case LogicalFilter(predicate=predicate, child=child):
            rewritten = _merge_adjacent_filters(child)
            return (
                plan
                if rewritten is child
                else LogicalFilter(plan.node_id, predicate, rewritten)
            )
        case LogicalProject(child=child):
            rewritten = _merge_adjacent_filters(child)
            return (
                plan
                if rewritten is child
                else LogicalProject(
                    plan.node_id, plan.projections, plan.output_columns, rewritten
                )
            )
    return plan


def _simplify_outer_joins(plan: LogicalNode) -> LogicalNode:
    """Turn an outer join into the smaller join it is, when a filter proves it.

    ``a LEFT JOIN b ON a.id = b.id WHERE b.x = 5`` is an inner join. The LEFT
    keeps every ``a`` row that found no partner, filling ``b``'s columns with
    NULLs; ``NULL = 5`` is NULL, a WHERE keeps only TRUE, and so not one of those
    rows can reach the output. Preserving them was work with no observer.

    The proof is :func:`~engine.optimizer.nullability.rejects_nulls`, and the
    rewrite is the same line four times, because a join is really two independent
    booleans rather than four names, see :meth:`JoinKind.of`::

        LEFT  ─(the right side is null-rejected)─▶  INNER
        RIGHT ─(the left side is null-rejected)──▶  INNER
        FULL  ─(the right side is null-rejected)─▶  RIGHT
        FULL  ─(the left side is null-rejected)──▶  LEFT
        FULL  ─(both)────────────────────────────▶  INNER

    **Why this rule earns its place.** It removes no operator and saves nothing
    on its own: an inner join and an outer join over the same inputs cost the
    same to run. What it does is hand the query back to the rest of the planner.
    Milestone 18 made an outer join a barrier that join reordering may not cross
    and a wall that predicate pushdown may not pass, both for good reasons. An
    inner join is neither. So the saving is second-hand and can be large: the
    two tables rejoin the search space, and the very predicate that proved the
    rewrite is now free to be pushed down to a scan and to use an index.

    **What proves it.** The WHERE, and also the ``ON`` of any *later* join in the
    chain that does not preserve its left input. An inner join's ``ON`` discards
    an accumulated row that fails it, and so does a ``RIGHT`` join's, which is
    what makes ``a LEFT JOIN b ON … JOIN c ON b.k = c.k`` an inner join too even
    with no WHERE at all. A ``LEFT`` or ``FULL`` join above preserves those rows,
    so its ``ON`` proves nothing and is not consulted. HAVING is not consulted
    either: it runs after grouping, and a NULL that survives into a group is a
    longer argument than this rule needs to make.

    The chain is walked **outermost first** so that a join already reduced this
    pass counts as its reduced kind for the ones inside it. A ``FULL`` that
    became a ``RIGHT`` starts discarding accumulated rows, and its ``ON`` becomes
    admissible evidence for the join beneath it.
    """
    if isinstance(plan, LogicalJoin):
        return _reduce_join_chain(plan, ())
    if isinstance(plan, LogicalFilter) and isinstance(plan.child, LogicalJoin):
        reduced = _reduce_join_chain(plan.child, (plan.predicate,))
        return (
            plan
            if reduced is plan.child
            else LogicalFilter(plan.node_id, plan.predicate, reduced)
        )

    child = getattr(plan, "child", None)
    if not isinstance(child, LogicalNode):
        return plan
    rewritten = _simplify_outer_joins(child)
    return plan if rewritten is child else replace(plan, child=rewritten)


def _reduce_join_chain(root: LogicalJoin, where: tuple[Expression, ...]) -> LogicalNode:
    chain = _written_order(root)
    if not any(join.kind.is_outer for join in chain):
        return root

    leftmost = chain[0].left
    if not isinstance(leftmost, LogicalScan) or not all(
        isinstance(join.right, LogicalScan) for join in chain
    ):
        # The planner builds left-deep chains of scans and this rule reads scan
        # positions off them. Anything else is a shape it has not been proved
        # against, so it is left alone rather than guessed at.
        return root

    kinds = _reduced_kinds(chain, leftmost.position, where)
    if kinds == [join.kind for join in chain]:
        return root

    node: LogicalNode = leftmost
    for join, kind in zip(chain, kinds, strict=True):
        node = replace(join, left=node, kind=kind)
    return node


def _written_order(root: LogicalJoin) -> list[LogicalJoin]:
    """The chain innermost first, which is the order the joins were typed in."""
    chain: list[LogicalJoin] = []
    node: LogicalNode = root
    while isinstance(node, LogicalJoin):
        chain.append(node)
        node = node.left
    chain.reverse()
    return chain


def _reduced_kinds(
    chain: list[LogicalJoin], first: int, where: tuple[Expression, ...]
) -> list[JoinKind]:
    """The kind each join in the chain can be reduced to. See the rule above."""
    rights = [join.right.position for join in chain]  # type: ignore[union-attr]

    # What sits to the left of each join: the first table, plus every table
    # joined before this one. That whole set is what a RIGHT join NULL-extends,
    # which is why it cannot be read off a single step.
    lefts: list[frozenset[int]] = []
    accumulated = {first}
    for position in rights:
        lefts.append(frozenset(accumulated))
        accumulated.add(position)

    kinds = [join.kind for join in chain]
    for index in reversed(range(len(chain))):
        if not kinds[index].is_outer:
            continue
        witnesses = list(where) + [
            chain[later].predicate
            for later in range(index + 1, len(chain))
            if not kinds[later].preserves_left
        ]
        kinds[index] = JoinKind.of(
            preserve_left=kinds[index].preserves_left
            and not rejects_nulls(witnesses, frozenset({rights[index]})),
            preserve_right=kinds[index].preserves_right
            and not rejects_nulls(witnesses, lefts[index]),
        )
    return kinds


def _scanned_schema(plan: LogicalNode):
    for node in (plan, *_descendants(plan)):
        if isinstance(node, LogicalScan):
            return node.schema
    return None


def _descendants(plan: LogicalNode) -> list[LogicalNode]:
    out: list[LogicalNode] = []
    for child in plan.children:
        out.append(child)
        out.extend(_descendants(child))
    return out


#: In order. Constant folding runs first because it is what creates the
#: ``WHERE TRUE`` the next rule removes, the one place order matters here.
RULES: tuple[Rule, ...] = (
    Rule(
        "fold_constants",
        "Evaluate subexpressions that do not depend on a row, once instead of per row",
        _fold_constants,
    ),
    Rule(
        "merge_adjacent_filters",
        "Combine stacked filters into one conjunction",
        _merge_adjacent_filters,
    ),
    Rule(
        "remove_trivial_filter",
        "Drop a WHERE clause that is constantly TRUE",
        _remove_trivial_filter,
    ),
    Rule(
        "drop_identity_projection",
        "Skip a projection that returns every column unchanged",
        _drop_identity_projection,
    ),
    Rule(
        "simplify_outer_joins",
        "Reduce an outer join whose preserved rows a filter above it cannot keep",
        _simplify_outer_joins,
    ),
)
