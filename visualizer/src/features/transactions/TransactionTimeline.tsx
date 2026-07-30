/**
 * Every transaction this handle has run, oldest first.
 *
 *   ● #1  implicit  committed   1 stmt   3 pages
 *   ● #2  implicit  committed   1 stmt   1 page
 *   ● #3  explicit  aborted     4 stmts  6 pages restored
 *   ◉ #4  explicit  active      2 stmts  5 pages held
 *
 * Most rows are implicit, and saying so matters: without the label the timeline
 * would look like the user had been typing BEGIN constantly, when in fact every
 * bare statement gets a transaction of its own. That is the single most
 * surprising thing about how this engine behaves after Milestone 8, and the
 * timeline is where it becomes obvious.
 *
 * The list is capped by the engine, not here, the manager keeps a bounded
 * history for the same reason the event ring buffer is bounded.
 */

import { Badge, EmptyState, type BadgeTone } from "@/components/primitives";
import { cn, formatCount, formatDuration } from "@/lib/format";
import type { TransactionModel } from "@/types/api";

const STATE_TONE: Record<string, BadgeTone> = {
  active: "accent",
  failed: "danger",
  committed: "heap",
  aborted: "danger",
};

export function TransactionTimeline({
  transactions,
  historyLimit,
}: {
  transactions: TransactionModel[];
  historyLimit: number;
}) {
  if (transactions.length === 0) {
    return (
      <EmptyState
        title="No transactions yet"
        hint="Run any statement. Even without BEGIN, the engine opens one and commits it."
      />
    );
  }

  const atLimit = transactions.length >= historyLimit;

  return (
    <div className="min-h-0 overflow-auto">
      <ul className="divide-y divide-[var(--border-subtle)]">
        {transactions.map((transaction) => (
          <Row key={transaction.transaction_id} transaction={transaction} />
        ))}
      </ul>
      {atLimit ? (
        <p className="text-muted px-2 py-1 text-[10px]">
          Only the last {historyLimit} finished transactions are kept.
        </p>
      ) : null}
    </div>
  );
}

function Row({ transaction }: { transaction: TransactionModel }) {
  const open = transaction.state === "active" || transaction.state === "failed";
  return (
    <li
      className={cn(
        "flex items-center gap-2 px-2 py-1.5 font-mono text-[11px]",
        open && "bg-[var(--accent)]/5",
      )}
    >
      <span className="text-muted w-10 shrink-0">
        #{transaction.transaction_id}
      </span>
      <Badge tone={STATE_TONE[transaction.state] ?? "neutral"}>
        {transaction.state}
      </Badge>
      <Badge
        tone="neutral"
        title={
          transaction.implicit
            ? "Opened by the engine around a bare statement, not by BEGIN"
            : "Opened by BEGIN"
        }
      >
        {transaction.implicit ? "implicit" : "explicit"}
      </Badge>
      <span className="text-muted">
        {formatCount(transaction.statements)} stmt
        {transaction.statements === 1 ? "" : "s"}
      </span>
      <span className="text-muted">{describePages(transaction)}</span>
      <span className="text-muted ml-auto">
        {formatDuration(transaction.duration_ns)}
      </span>
    </li>
  );
}

function describePages(transaction: TransactionModel): string {
  if (transaction.state === "aborted") {
    return `${formatCount(transaction.pages_restored)} restored`;
  }
  if (transaction.state === "active" || transaction.state === "failed") {
    return `${formatCount(transaction.pages_held)} held`;
  }
  return `${formatCount(transaction.pages_written)} write${
    transaction.pages_written === 1 ? "" : "s"
  }`;
}
