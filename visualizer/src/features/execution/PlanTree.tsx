/**
 * The physical operator tree, with actual statistics.
 *
 *     Project   email, (age * 2)          in 2  out 2
 *     └─ Filter   (age >= 18)             in 4  out 2   rejected 2
 *        └─ SeqScan   table=users         out 4
 *
 * Drawn root-first, which is how the data *flows* — rows come out of the top —
 * even though execution *pulls* downward. That is the volcano model's one
 * genuinely confusing property, so the arrows are labelled explicitly.
 *
 * When an execution is paused, the operator at the checkpoint is highlighted, so
 * you can see which node the engine is actually sitting in.
 *
 * From Milestone 6 each row also carries what the planner *expected*. The gap
 * between estimated and actual rows is where a slow query's explanation almost
 * always is, so it is shown inline and flagged once it exceeds 2x in either
 * direction — the same reason PostgreSQL's `EXPLAIN ANALYZE` prints
 * `rows=100 ... actual rows=90000` on one line.
 */

import { Badge, EmptyState, Panel } from "@/components/primitives";
import { cn, formatCount, formatDuration } from "@/lib/format";
import type {
  PlanAlternativeModel,
  OperatorNodeModel,
  PlanModel,
  PlanStatisticsModel,
} from "@/types/api";

const OPERATOR_TONE: Record<string, string> = {
  SeqScan: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  IndexScan: "bg-violet-500/15 text-violet-700 dark:text-violet-300",
  Filter: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  Project: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  // The two-input and the blocking operators share a tone each, so the shape
  // of a plan is readable before any of the text is: pink where the row count
  // can multiply, orange where the pipeline stops.
  HashJoin: "bg-pink-500/15 text-pink-700 dark:text-pink-300",
  NestedLoopJoin: "bg-pink-500/15 text-pink-700 dark:text-pink-300",
  HashAggregate: "bg-orange-500/15 text-orange-700 dark:text-orange-300",
  Sort: "bg-orange-500/15 text-orange-700 dark:text-orange-300",
  Limit: "bg-zinc-500/15",
};

/** How far an estimate may drift before it is worth pointing at. */
const ESTIMATE_TOLERANCE = 2;

/**
 * How wrong the row estimate was, as a multiple, or null when there is nothing
 * to compare. Symmetric: over-estimating by 10x and under-estimating by 10x are
 * equally bad, and both come out as 10.
 */
function estimateError(
  estimated: number | null,
  actual: number,
): number | null {
  if (estimated === null || estimated <= 0) return null;
  if (actual === 0) return estimated > ESTIMATE_TOLERANCE ? estimated : null;
  return Math.max(actual / estimated, estimated / actual);
}

export function PlanTree({
  plan,
  activeOperatorId,
  onSelectOperator,
  selectedOperatorId,
}: {
  plan: PlanModel | null | undefined;
  /** The operator the engine is paused inside, if any. */
  activeOperatorId?: string | null;
  onSelectOperator?: (operatorId: string | null) => void;
  selectedOperatorId?: string | null;
}) {
  if (!plan || plan.nodes.length === 0) {
    return (
      <EmptyState
        title="No plan"
        hint="INSERT and CREATE TABLE have no operator tree — they call straight into the storage engine. Run a SELECT to see one."
      />
    );
  }

  const byId = new Map(plan.nodes.map((node) => [node.operator_id, node]));

  return (
    <div className="p-3">
      <p className="text-muted mb-2 text-[10px] tracking-wide uppercase">
        rows flow up · next() calls travel down
      </p>
      <PlanRow
        operatorId={plan.root_id}
        byId={byId}
        depth={0}
        activeOperatorId={activeOperatorId ?? null}
        selectedOperatorId={selectedOperatorId ?? null}
        onSelectOperator={onSelectOperator}
      />
    </div>
  );
}

function PlanRow({
  operatorId,
  byId,
  depth,
  activeOperatorId,
  selectedOperatorId,
  onSelectOperator,
}: {
  operatorId: string;
  byId: Map<string, OperatorNodeModel>;
  depth: number;
  activeOperatorId: string | null;
  selectedOperatorId: string | null;
  onSelectOperator?: (operatorId: string | null) => void;
}) {
  const node = byId.get(operatorId);
  if (!node) return null;

  const active = activeOperatorId === operatorId;
  const selected = selectedOperatorId === operatorId;

  return (
    <>
      <button
        type="button"
        onClick={() => onSelectOperator?.(selected ? null : operatorId)}
        aria-pressed={selected}
        aria-current={active ? "step" : undefined}
        aria-label={`${node.operator_type} ${node.operator_id}, ${node.output_rows} rows out`}
        className={cn(
          "flex w-full items-baseline gap-2 rounded px-2 py-1 text-left transition-colors",
          active
            ? "ring-2 ring-[var(--accent)] ring-inset"
            : selected
              ? "bg-[var(--accent)]/12"
              : "hover:bg-[var(--surface-sunken)]",
        )}
        style={{ paddingLeft: `${8 + depth * 20}px` }}
      >
        {depth > 0 ? (
          <span aria-hidden className="text-muted shrink-0 opacity-50">
            └─
          </span>
        ) : null}
        <span
          className={cn(
            "shrink-0 rounded px-1.5 py-0.5 text-[11px] font-semibold",
            OPERATOR_TONE[node.operator_type] ?? "bg-zinc-500/15",
          )}
        >
          {node.operator_type}
        </span>
        <span className="text-muted min-w-0 flex-1 truncate font-mono text-[11px]">
          {node.detail}
        </span>

        <span
          className="shrink-0 font-mono text-[10px]"
          title={`${node.input_rows} rows in, ${node.output_rows} out, over ${node.next_calls} next() calls`}
        >
          <span className="text-muted">in</span> {formatCount(node.input_rows)}{" "}
          <span className="text-muted">out</span> {formatCount(node.output_rows)}
        </span>
        {node.rows_rejected > 0 ? (
          <Badge
            tone="danger"
            title="Rows whose predicate was not exactly TRUE — including NULL, which is not TRUE"
          >
            −{formatCount(node.rows_rejected)}
          </Badge>
        ) : null}
        <EstimateBadge node={node} />
        <span className="text-muted w-16 shrink-0 text-right font-mono text-[10px]">
          {formatDuration(node.duration_ns)}
        </span>
      </button>

      {node.children.map((childId) => (
        <PlanRow
          key={childId}
          operatorId={childId}
          byId={byId}
          depth={depth + 1}
          activeOperatorId={activeOperatorId}
          selectedOperatorId={selectedOperatorId}
          onSelectOperator={onSelectOperator}
        />
      ))}
    </>
  );
}

/**
 * The planner's estimate beside what happened.
 *
 * Silent when there is no estimate (a plan built before Milestone 6, or an
 * execution that never ran) and quiet when the estimate was close — a badge on
 * every row would train you to ignore it.
 */
function EstimateBadge({ node }: { node: OperatorNodeModel }) {
  if (node.estimated_rows === null) return null;
  const error = estimateError(node.estimated_rows, node.output_rows);
  const wrong = error !== null && error >= ESTIMATE_TOLERANCE;
  const title =
    `planner expected ${node.estimated_rows} row(s), got ${node.output_rows}` +
    (node.estimated_cost !== null
      ? `; estimated cost ${node.estimated_cost.toFixed(1)}`
      : "") +
    (wrong
      ? `. Off by ${error!.toFixed(1)}x — a bad row estimate is where a bad plan usually comes from.`
      : "");

  return (
    <span
      className={cn(
        "shrink-0 font-mono text-[10px]",
        wrong ? "text-amber-600 dark:text-amber-400" : "text-muted",
      )}
      title={title}
    >
      est {formatCount(Math.round(node.estimated_rows))}
      {wrong ? ` (${error!.toFixed(1)}x off)` : ""}
    </span>
  );
}

/** Every access path the planner weighed, and what each would have cost. */
export function AlternativesPanel({ plan }: { plan: PlanModel | null | undefined }) {
  if (!plan || plan.alternatives.length === 0) {
    return (
      <EmptyState
        title="No alternatives"
        hint="Run a SELECT to see what the planner considered."
      />
    );
  }

  // Grouped by which question each answered. A query over one table makes one
  // decision and the grouping is invisible; a join makes several, and a flat
  // list of them reads as a contradiction — three entries marked "chosen".
  const groups = new Map<string, PlanAlternativeModel[]>();
  for (const alternative of plan.alternatives) {
    const key = alternative.decision || "access path";
    groups.set(key, [...(groups.get(key) ?? []), alternative]);
  }

  return (
    <div className="space-y-3 p-3">
      {[...groups].map(([decision, considered]) => {
        const cheapest = Math.min(...considered.map((a) => a.estimated_cost));
        return (
          <div key={decision}>
            {groups.size > 1 ? (
              <p className="text-muted mb-1 text-[10px] tracking-wide uppercase">
                {decision}
              </p>
            ) : null}
            <ul className="space-y-1.5">
              {considered.map((alternative, index) => (
                <li
                  key={`${alternative.description}-${index}`}
                  className={cn(
                    "rounded border px-2.5 py-1.5",
                    alternative.chosen
                      ? "border-[var(--accent)] bg-[var(--accent)]/8"
                      : "border-[var(--border-subtle)] opacity-70",
                  )}
                >
                  <div className="flex items-baseline gap-2">
                    <span className="text-[10px]">
                      {alternative.chosen ? "▶" : "○"}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-xs font-medium">
                      {alternative.description}
                    </span>
                    <span
                      className="shrink-0 font-mono text-[11px]"
                      title="Estimated cost. The unit is one page read; see engine/optimizer/cost.py."
                    >
                      {alternative.estimated_cost.toFixed(1)}
                    </span>
                  </div>
                  <p className="text-muted mt-0.5 pl-5 font-mono text-[10px]">
                    {formatCount(Math.round(alternative.estimated_rows))} row(s)
                    expected
                    {alternative.rejected_because
                      ? ` · ${alternative.rejected_because}`
                      : cheapest === alternative.estimated_cost
                        ? " · cheapest"
                        : ""}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        );
      })}

      {plan.rewrites.length > 0 ? (
        <div>
          <p className="text-muted mb-1 text-[10px] tracking-wide uppercase">
            rewrites applied
          </p>
          <div className="flex flex-wrap gap-1">
            {plan.rewrites.map((rule) => (
              <Badge key={rule} tone="neutral">
                {rule}
              </Badge>
            ))}
          </div>
        </div>
      ) : null}

      {plan.statistics ? <StatisticsNote statistics={plan.statistics} /> : null}
    </div>
  );
}

function StatisticsNote({ statistics }: { statistics: PlanStatisticsModel }) {
  return (
    <div>
      <p className="text-muted mb-1 text-[10px] tracking-wide uppercase">
        statistics used
      </p>
      <p className="text-muted font-mono text-[10px]">
        {statistics.table_name}: {formatCount(statistics.row_count)} rows,{" "}
        {statistics.page_count} pages
      </p>
      {statistics.stale ? (
        <p
          className="mt-1 rounded bg-amber-500/10 px-2 py-1 text-[10px] text-amber-700 dark:text-amber-400"
          title="Statistics are not refreshed on every write — that would cost a full scan per row. Run ANALYZE."
        >
          Stale: the table has been written to since ANALYZE, so every estimate
          above is based on the numbers shown. Run <code>ANALYZE</code> to refresh.
        </p>
      ) : null}
    </div>
  );
}

/** The operator tree plus its own panel chrome, for the results column. */
export function PlanPanel({
  plan,
  activeOperatorId,
  subtitle,
  actions,
}: {
  plan: PlanModel | null | undefined;
  activeOperatorId?: string | null;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  const cost =
    plan?.estimated_cost != null ? `est. cost ${plan.estimated_cost.toFixed(1)}` : undefined;
  return (
    <Panel
      title="Plan"
      subtitle={subtitle ?? cost}
      className="h-full"
      actions={actions}
    >
      <PlanTree plan={plan} activeOperatorId={activeOperatorId} />
      {plan && plan.alternatives.length > 0 ? (
        <div className="mt-2 border-t border-[var(--border-subtle)]">
          <p className="text-muted px-3 pt-2 text-[10px] tracking-wide uppercase">
            what the planner considered
          </p>
          <AlternativesPanel plan={plan} />
        </div>
      ) : null}
    </Panel>
  );
}
