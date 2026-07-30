/**
 * What the last open had to repair.
 *
 *   analysis   6 transactions   →   5 finished, 1 interrupted
 *   redo       73 replayed, 211 already current
 *   undo       11 pages put back
 *
 * The three phases in order, because the order *is* the algorithm: redo replays
 * everything including the interrupted transaction's work, and only then does
 * undo take that part back. Showing them as a flat list of numbers would lose
 * the one thing about ARIES that surprises people.
 *
 * "already current" is given equal billing with "replayed" on purpose. A record
 * skipped because the page had already got it is the last checkpoint paying for
 * itself, and a panel that only counted work done would make checkpoints look
 * like they achieved nothing.
 */

import { Badge, EmptyState } from "@/components/primitives";
import { formatCount, formatDuration } from "@/lib/format";
import type { RecoveryReportModel } from "@/types/api";

export function RecoveryPanel({ report }: { report: RecoveryReportModel }) {
  if (!report.ran) {
    return (
      <EmptyState
        title="Nothing to recover"
        hint="The last process shut down cleanly, which ends with a checkpoint and leaves an empty log. That is what makes this panel meaningful when it is not empty."
      />
    );
  }

  return (
    <div className="space-y-3 p-3 text-[11px]">
      <p className="font-mono">{report.summary}</p>

      <div className="space-y-2">
        <Phase
          name="analysis"
          ns={report.phase_ns.analysis ?? 0}
          detail={`${formatCount(report.records_scanned)} record(s) scanned`}
        >
          <span className="text-muted">
            {report.winners.length > 0 ? (
              <>finished {report.winners.map((id) => `#${id}`).join(", ")}</>
            ) : (
              "nothing finished"
            )}
            {report.losers.length > 0 ? (
              <>
                {" · "}
                <span className="text-red-600 dark:text-red-400">
                  interrupted {report.losers.map((id) => `#${id}`).join(", ")}
                </span>
              </>
            ) : null}
          </span>
        </Phase>

        <Phase
          name="redo"
          ns={report.phase_ns.redo ?? 0}
          detail={`${formatCount(report.pages_redone)} replayed`}
        >
          <span className="text-muted">
            {formatCount(report.pages_skipped)} already current: the page's own
            LSN was past the record's
          </span>
        </Phase>

        <Phase
          name="undo"
          ns={report.phase_ns.undo ?? 0}
          detail={`${formatCount(report.pages_undone)} put back`}
        >
          <span className="text-muted">
            {report.losers.length === 0
              ? "nothing to undo; every transaction finished"
              : "the interrupted transactions' pages, restored from their before-images"}
          </span>
        </Phase>
      </div>

      {report.truncated_tail ? (
        <p className="text-muted">
          The last record in the log was incomplete. The process died part-way
          through writing it. Expected after a crash, and not corruption.
        </p>
      ) : null}

      <p className="text-muted">
        Total {formatDuration(report.duration_ns)}. Recovery runs on open,
        before anything can read a page, so a database is never observed
        half-repaired.
      </p>
    </div>
  );
}

function Phase({
  name,
  ns,
  detail,
  children,
}: {
  name: string;
  ns: number;
  detail: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-2 font-mono">
      <Badge tone="accent" className="w-[68px] justify-center">
        {name}
      </Badge>
      <span className="shrink-0">{detail}</span>
      <span className="min-w-0 flex-1 truncate">{children}</span>
      <span className="text-muted shrink-0">{formatDuration(ns)}</span>
    </div>
  );
}
