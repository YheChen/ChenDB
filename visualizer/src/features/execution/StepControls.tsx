/**
 * Step-through controls and the current checkpoint.
 *
 * Every button maps to one API call on a real execution that is genuinely
 * blocked on a condition variable inside the engine — nothing here is simulated
 * or replayed from a recording.
 *
 *   Step            pause at the very next checkpoint of any kind
 *   Next row        run until a row comes out of the top of the tree
 *   Next page read  run until the storage engine reads a page
 *   Continue        run to completion
 *   Cancel          unwind the operator tree and release the database lock
 *
 * Cancel is always enabled while an execution is live, because a paused query
 * holds its database's lock and cancelling is the only way to get it back.
 */

import { Badge, Button } from "@/components/primitives";
import { cn, formatCount } from "@/lib/format";
import type { ResumeModeName } from "@/lib/api";
import type { ExecutionDetail } from "@/types/api";

const STATE_TONE: Record<string, string> = {
  pending: "bg-zinc-500/15 text-zinc-500",
  running: "bg-amber-500/15 text-amber-600",
  paused: "bg-sky-500/15 text-sky-600 dark:text-sky-300",
  finished: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-300",
  cancelled: "bg-zinc-500/15 text-zinc-500",
  failed: "bg-red-500/15 text-red-600 dark:text-red-300",
};

/** What each checkpoint kind means, shown as a tooltip. */
const KIND_HELP: Record<string, string> = {
  operator_open:
    "An operator acquired its resources. Children open before their parents.",
  operator_next:
    "An operator was asked for a row. These travel DOWN the tree: the root asks its child, which asks its own child.",
  row_emitted:
    "A row came OUT of an operator. These travel UP: the scan produces, the filter passes it on, the projection reshapes it.",
  operator_close: "An operator released its resources.",
  page_read:
    "The storage engine read a page from disk. Reported through the diagnostics bus, so the pager knows nothing about stepping.",
};

const RESUME_BUTTONS: { mode: ResumeModeName; label: string; title: string }[] = [
  { mode: "step", label: "Step", title: "Pause at the next checkpoint of any kind" },
  { mode: "until_row", label: "Next row", title: "Run until a row is emitted" },
  {
    mode: "until_page_read",
    label: "Next page",
    title: "Run until the storage engine reads a page",
  },
];

export function StepControls({
  execution,
  isPending,
  onStart,
  onResume,
  onCancel,
  canStart,
}: {
  execution: ExecutionDetail | null;
  isPending: boolean;
  onStart: () => void;
  onResume: (mode: ResumeModeName) => void;
  onCancel: () => void;
  canStart: boolean;
}) {
  const live = execution !== null && execution.state === "paused";
  const terminal =
    execution !== null &&
    ["finished", "cancelled", "failed"].includes(execution.state);

  return (
    <div className="flex flex-col gap-2 border-b border-[var(--border-subtle)] p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          variant="primary"
          onClick={onStart}
          disabled={!canStart || isPending || live}
          title="Start a stepped execution, paused at its first checkpoint"
        >
          ▶ Start stepping
        </Button>

        <span className="mx-1 h-5 w-px bg-[var(--border-subtle)]" />

        {RESUME_BUTTONS.map((button) => (
          <Button
            key={button.mode}
            onClick={() => onResume(button.mode)}
            disabled={!live || isPending}
            title={button.title}
          >
            {button.label}
          </Button>
        ))}
        <Button
          onClick={() => onResume("continue")}
          disabled={!live || isPending}
          title="Run to completion without pausing"
        >
          ⏭ Continue
        </Button>
        <Button
          variant="danger"
          onClick={onCancel}
          disabled={execution === null || terminal || isPending}
          title="Unwind the operator tree and release the database lock"
        >
          ✕ Cancel
        </Button>

        <div className="flex-1" />

        {execution ? (
          <>
            <span className="text-muted font-mono text-[10px]">
              {formatCount(execution.steps_taken)} steps
            </span>
            <span
              className={cn(
                "rounded px-1.5 py-0.5 font-mono text-[10px]",
                STATE_TONE[execution.state] ?? "bg-zinc-500/15",
              )}
            >
              {execution.state}
            </span>
          </>
        ) : null}
      </div>

      {execution?.pause_kind ? (
        <div className="surface-sunken flex flex-wrap items-baseline gap-2 rounded p-2">
          <Badge tone="accent" title={KIND_HELP[execution.pause_kind]}>
            {execution.pause_kind}
          </Badge>
          {execution.pause_operator_id ? (
            <span className="font-mono text-[11px] font-semibold">
              {execution.pause_operator_id}
            </span>
          ) : null}
          <span className="text-muted min-w-0 flex-1 truncate font-mono text-[11px]">
            {execution.pause_detail}
          </span>
          <span className="text-muted font-mono text-[10px]">
            {formatCount(execution.rows_so_far)} rows out
          </span>
        </div>
      ) : null}

      {execution?.error ? (
        <p
          role="alert"
          className="rounded bg-red-500/10 p-2 font-mono text-[11px] text-red-600 dark:text-red-300"
        >
          {execution.error}
        </p>
      ) : null}

      {execution === null ? (
        <p className="text-muted text-[11px]">
          Stepping runs the query on its own thread and blocks it at each
          checkpoint. Watch <code>next()</code> travel down the tree and rows
          travel back up.
        </p>
      ) : null}
    </div>
  );
}
