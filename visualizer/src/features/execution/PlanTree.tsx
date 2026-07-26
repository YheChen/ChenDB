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
 */

import { Badge, EmptyState, Panel } from "@/components/primitives";
import { cn, formatCount, formatDuration } from "@/lib/format";
import type { OperatorNodeModel, PlanModel } from "@/types/api";

const OPERATOR_TONE: Record<string, string> = {
  SeqScan: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  Filter: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  Project: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
};

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
  return (
    <Panel title="Plan" subtitle={subtitle} className="h-full" actions={actions}>
      <PlanTree plan={plan} activeOperatorId={activeOperatorId} />
    </Panel>
  );
}
