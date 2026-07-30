/**
 * The lock table and the wait-for graph.
 *
 *   users:4.1   held by #7 exclusive        waiting: #9
 *   users:4.2   held by #9 exclusive        waiting: #7
 *
 *   wait-for:   #7 ──▶ #9 ──▶ #7      a cycle. Somebody loses.
 *
 * **Every row here is a writer.** Under MVCC a reader takes no lock at all, so
 * an empty table during a heavy read workload is the feature working rather
 * than the panel failing, which is why the counters spell out
 * "readers blocked: 0" instead of leaving it to be inferred.
 *
 * The graph is drawn as arrows rather than a node diagram. A cycle among two or
 * three transactions is legible as text and a force-directed layout of four
 * nodes is not, and the deadlock detector breaks cycles as they form, so what
 * is usually on screen is a chain rather than a loop.
 */

import { Badge, EmptyState } from "@/components/primitives";
import { formatCount } from "@/lib/format";
import type { LockTableResponse } from "@/types/api";

export function LockTable({ locks }: { locks: LockTableResponse }) {
  if (locks.entries.length === 0) {
    return (
      <EmptyState
        title="Nothing is locked"
        hint="Which is the usual state. A reader never takes a lock; only two writers on the same row need one."
      />
    );
  }

  return (
    <div className="min-h-0 overflow-auto">
      <table className="w-full border-collapse font-mono text-[11px]">
        <thead className="bg-[var(--surface-sunken)] sticky top-0">
          <tr className="text-muted text-left">
            <th className="px-2 py-1 font-medium">Resource</th>
            <th className="px-2 py-1 font-medium">Held by</th>
            <th className="px-2 py-1 font-medium">Waiting</th>
          </tr>
        </thead>
        <tbody>
          {locks.entries.map((entry) => (
            <tr
              key={entry.resource}
              className="border-t border-[var(--border-subtle)]"
            >
              <td className="px-2 py-1" title="table : page . slot, one row">
                {entry.resource}
              </td>
              <td className="px-2 py-1">
                {Object.entries(entry.holders).map(([txn, mode]) => (
                  <Badge
                    key={txn}
                    tone={mode === "exclusive" ? "danger" : "neutral"}
                    className="mr-1"
                  >
                    #{txn} {mode === "exclusive" ? "X" : "S"}
                  </Badge>
                ))}
              </td>
              <td className="px-2 py-1">
                {entry.waiters.length === 0 ? (
                  <span className="text-muted">—</span>
                ) : (
                  entry.waiters.map((txn) => (
                    <Badge key={txn} tone="accent" className="mr-1">
                      #{txn}
                    </Badge>
                  ))
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {locks.wait_for.length > 0 ? (
        <div className="border-t border-[var(--border-subtle)] px-2 py-1.5 font-mono text-[11px]">
          <span className="text-muted">wait-for </span>
          {locks.wait_for.map((edge) => (
            <span key={edge.waiter} className="mr-3">
              #{edge.waiter} ▶ {edge.blockers.map((b) => `#${b}`).join(", ")}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function LockCounters({ locks }: { locks: LockTableResponse }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 p-3 font-mono text-[11px] sm:grid-cols-5">
      <Stat label="granted" value={formatCount(locks.stats.granted)} />
      <Stat
        label="waits"
        value={formatCount(locks.stats.waits)}
        hint="Requests that had to block. Against 'granted' this is how much contention there actually is, rather than how much the design permits."
      />
      <Stat
        label="deadlocks"
        value={formatCount(locks.stats.deadlocks)}
        hint="Cycles found in the wait-for graph. Each one cost the youngest transaction in it a rollback."
      />
      <Stat label="timeouts" value={formatCount(locks.stats.timeouts)} />
      <Stat
        label="readers blocked"
        value={String(locks.readers_blocked)}
        hint="Always zero, and shown so the zero is visible. Under MVCC a reader takes no lock, so nothing could block one."
      />
    </dl>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div title={hint}>
      <dt className="text-muted text-[10px] tracking-wide uppercase">
        {label}
      </dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}
