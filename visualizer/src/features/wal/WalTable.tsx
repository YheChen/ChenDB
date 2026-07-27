/**
 * The log as what it is: a list, in order, that only ever grows at one end.
 *
 *   LSN      txn   type        page   size    ↩ prev
 *   0        1     update      4      1 KiB   —
 *   1068     1     update      7      552 B   0
 *   1620     1     commit      —      44 B    1068
 *
 * Newest **last**, unlike the undo log and unlike the event timeline. A log is
 * read forwards — recovery replays it forwards, and the `prev_lsn` chain only
 * makes sense pointing backwards up the page. Reversing it to match the other
 * panels would make the one thing this view is for harder to see.
 *
 * `prev_lsn` is drawn because it is the part of an ARIES record people have
 * usually read about and never seen: each record points at the same
 * transaction's previous one, so a transaction is a chain threaded through a
 * file that is otherwise strictly chronological.
 */

import { Badge, EmptyState, type BadgeTone } from "@/components/primitives";
import { cn, formatBytes, formatCount } from "@/lib/format";
import type { WalRecordModel, WalResponse } from "@/types/api";

const TYPE_TONE: Record<string, BadgeTone> = {
  update: "neutral",
  commit: "heap",
  abort: "danger",
  checkpoint: "accent",
};

export function WalTable({
  wal,
  onSelectPage,
}: {
  wal: WalResponse;
  onSelectPage?: (pageId: number) => void;
}) {
  if (!wal.enabled) {
    return (
      <EmptyState
        title="No log"
        hint="This database was opened without a write-ahead log."
      />
    );
  }
  if (wal.records.length === 0) {
    return (
      <EmptyState
        title="The log is empty"
        hint="Which is what a clean shutdown or a fresh checkpoint leaves. Write a row and it starts filling."
      />
    );
  }

  const hidden = wal.total_records - wal.records.length;

  return (
    <div className="min-h-0 overflow-auto">
      {hidden > 0 ? (
        <p className="text-muted bg-[var(--surface-sunken)] px-2 py-1 text-[10px]">
          Showing the last {formatCount(wal.records.length)} of{" "}
          {formatCount(wal.total_records)} records.
        </p>
      ) : null}
      <table className="w-full border-collapse font-mono text-[11px]">
        <thead className="bg-[var(--surface-sunken)] sticky top-0">
          <tr className="text-muted text-left">
            <th className="px-2 py-1 font-medium">LSN</th>
            <th className="px-2 py-1 font-medium">Txn</th>
            <th className="px-2 py-1 font-medium">Type</th>
            <th className="px-2 py-1 font-medium">Page</th>
            <th className="px-2 py-1 font-medium">Size</th>
            <th className="px-2 py-1 font-medium">Undo</th>
            <th className="px-2 py-1 font-medium">↩ prev</th>
          </tr>
        </thead>
        <tbody>
          {wal.records.map((record) => (
            <Row
              key={record.lsn}
              record={record}
              durable={record.lsn < wal.flushed_lsn}
              onSelectPage={onSelectPage}
            />
          ))}
        </tbody>
      </table>
      {wal.truncated_tail ? (
        <p className="px-2 py-1 text-[10px] text-amber-600 dark:text-amber-400">
          The last record in the file is incomplete — the process died part-way
          through writing it. Recovery stops here, which is correct.
        </p>
      ) : null}
      {wal.buffered_bytes > 0 ? (
        <p className="text-muted px-2 py-1 text-[10px]">
          …and {formatBytes(wal.buffered_bytes)} still staged in memory, not yet
          written. A crash right now would lose exactly that much.
        </p>
      ) : null}
    </div>
  );
}

function Row({
  record,
  durable,
  onSelectPage,
}: {
  record: WalRecordModel;
  durable: boolean;
  onSelectPage?: (pageId: number) => void;
}) {
  const isUpdate = record.record_type === "update";
  return (
    <tr
      className={cn(
        "border-t border-[var(--border-subtle)]",
        !durable && "opacity-60",
      )}
      title={durable ? undefined : "Staged in memory — not on the disk yet"}
    >
      <td className="text-muted px-2 py-1 tabular-nums">{record.lsn}</td>
      <td className="px-2 py-1 tabular-nums">
        {record.transaction_id === 0 ? (
          <span
            className="text-muted"
            title="Engine bookkeeping, outside any transaction"
          >
            —
          </span>
        ) : (
          `#${record.transaction_id}`
        )}
      </td>
      <td className="px-2 py-1">
        <Badge tone={TYPE_TONE[record.record_type] ?? "neutral"}>
          {record.record_type}
        </Badge>
      </td>
      <td className="px-2 py-1">
        {isUpdate && onSelectPage ? (
          <button
            type="button"
            onClick={() => onSelectPage(record.page_id)}
            className="text-[var(--accent)] hover:underline"
            title="Open this page in the inspector"
          >
            {record.page_id}
          </button>
        ) : isUpdate ? (
          record.page_id
        ) : (
          <span className="text-muted">—</span>
        )}
      </td>
      <td className="px-2 py-1">{formatBytes(record.size)}</td>
      <td className="px-2 py-1">
        {record.before_image_size > 0 ? (
          <span title="The transaction's first write to this page, so it carries the image to roll back to">
            {formatBytes(record.before_image_size)}
          </span>
        ) : (
          <span
            className="text-muted"
            title="Redo only — this page was already captured"
          >
            —
          </span>
        )}
      </td>
      <td className="text-muted px-2 py-1 tabular-nums">
        {record.prev_lsn > 0 ? record.prev_lsn : "—"}
      </td>
    </tr>
  );
}

export function WalCounters({ wal }: { wal: WalResponse }) {
  const commitsPerSecond =
    wal.stats.mean_sync_ns > 0 ? 1e9 / wal.stats.mean_sync_ns : 0;
  const avoided =
    wal.stats.records_appended + wal.stats.records_coalesced > 0
      ? wal.stats.records_coalesced /
        (wal.stats.records_appended + wal.stats.records_coalesced)
      : 0;

  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 p-3 font-mono text-[11px] sm:grid-cols-4">
      <Stat
        label="log size"
        value={formatBytes(wal.size_bytes)}
        hint="On disk. A checkpoint takes this to zero."
      />
      <Stat
        label="records"
        value={formatCount(wal.total_records)}
        hint="In the file right now, not since the handle opened"
      />
      <Stat
        label="fsync"
        value={
          wal.stats.syncs > 0
            ? `${(wal.stats.mean_sync_ns / 1000).toFixed(0)} µs`
            : "—"
        }
        hint="Average, over every commit. This is the expensive call."
      />
      <Stat
        label="commit ceiling"
        value={
          commitsPerSecond > 0
            ? `${formatCount(Math.round(commitsPerSecond))}/s`
            : "—"
        }
        hint="One second divided by the fsync. Nothing about how much work each transaction did — which is why real systems batch commits."
      />
      <Stat
        label="images avoided"
        value={avoided > 0 ? `${(avoided * 100).toFixed(0)}%` : "0%"}
        hint="Writes that replaced a staged record for the same page instead of adding one. A bulk insert is almost all of them."
      />
      <Stat label="syncs" value={formatCount(wal.stats.syncs)} />
      <Stat label="checkpoints" value={formatCount(wal.stats.checkpoints)} />
      <Stat
        label="reclaimed"
        value={formatBytes(wal.stats.bytes_reclaimed)}
        hint="Log bytes discarded by checkpoints"
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
