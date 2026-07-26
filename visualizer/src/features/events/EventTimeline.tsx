/**
 * The live event timeline.
 *
 * Diagnostic events arrive over the WebSocket as the engine works. Each row is
 * one thing the storage engine actually did — a page read, an allocation, a
 * record insert — with the numbers it reported.
 */

import { useMemo, useState } from "react";
import { Button, EmptyState, Panel } from "@/components/primitives";
import type { ConnectionState } from "@/hooks/useEventStream";
import { cn, formatDuration, formatTimestamp } from "@/lib/format";
import type { TraceRecordModel } from "@/types/api";

const CATEGORY_TONE: Record<string, string> = {
  lifecycle: "bg-violet-500/15 text-violet-600 dark:text-violet-300",
  storage: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  record: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
};

const CONNECTION_LABEL: Record<ConnectionState, { text: string; tone: string }> = {
  idle: { text: "idle", tone: "bg-zinc-500/15 text-zinc-500" },
  connecting: { text: "connecting", tone: "bg-amber-500/15 text-amber-600" },
  open: { text: "live", tone: "bg-emerald-500/15 text-emerald-600" },
  reconnecting: { text: "reconnecting", tone: "bg-amber-500/15 text-amber-600" },
  closed: { text: "disconnected", tone: "bg-red-500/15 text-red-600" },
  error: { text: "error", tone: "bg-red-500/15 text-red-600" },
};

/** Fields worth surfacing inline, in the order they read best. */
const SUMMARY_KEYS = [
  "page_id",
  "slot_id",
  "file_offset",
  "length",
  "page_type",
  "action",
  "source",
  "rows_emitted",
  "pages_scanned",
  "reclaimed_bytes",
  "duration_ns",
];

function summarize(event: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const key of SUMMARY_KEYS) {
    const value = event[key];
    if (value === undefined || value === null) continue;
    if (key === "duration_ns" && typeof value === "number") {
      parts.push(formatDuration(value));
    } else {
      parts.push(`${key}=${value}`);
    }
  }
  return parts.join("  ");
}

export function EventTimeline({
  events,
  connection,
  droppedByServer,
  droppedByClient,
  totalReceived,
  paused,
  onTogglePause,
  onClear,
}: {
  events: TraceRecordModel[];
  connection: ConnectionState;
  droppedByServer: number;
  droppedByClient: number;
  totalReceived: number;
  paused: boolean;
  onTogglePause: () => void;
  onClear: () => void;
}) {
  const [filter, setFilter] = useState<string>("all");

  const categories = useMemo(
    () => ["all", ...new Set(events.map((event) => event.category))],
    [events],
  );

  const visible = useMemo(() => {
    const filtered =
      filter === "all" ? events : events.filter((event) => event.category === filter);
    // Newest first: during a scan the interesting event is the latest one.
    return filtered.slice(-500).reverse();
  }, [events, filter]);

  const status = CONNECTION_LABEL[connection];
  const dropped = droppedByServer + droppedByClient;

  return (
    <Panel
      title="Event timeline"
      subtitle={`${totalReceived.toLocaleString()} received${dropped > 0 ? ` · ${dropped.toLocaleString()} dropped` : ""}`}
      className="h-full"
      actions={
        <>
          <span
            className={cn("rounded px-1.5 py-0.5 font-mono text-[10px]", status.tone)}
            title={
              connection === "open"
                ? "Streaming live from the engine"
                : "The engine is not sending events right now"
            }
          >
            {status.text}
          </span>
          <select
            aria-label="Filter events by category"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            className="surface-sunken rounded border border-[var(--border-subtle)] px-1.5 py-0.5 text-[11px]"
          >
            {categories.map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </select>
          <Button onClick={onTogglePause} aria-pressed={paused}>
            {paused ? "Resume" : "Pause"}
          </Button>
          <Button onClick={onClear}>Clear</Button>
        </>
      }
    >
      {dropped > 0 ? (
        <p
          className="border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-700 dark:text-amber-300"
          role="status"
        >
          {dropped.toLocaleString()} events were dropped
          {droppedByServer > 0 ? ` (${droppedByServer.toLocaleString()} by the server's backpressure policy)` : ""}
          . The timeline below has gaps — lower the trace level to keep up.
        </p>
      ) : null}

      {visible.length === 0 ? (
        <EmptyState
          title={paused ? "Paused" : "No events yet"}
          hint={
            paused
              ? "Resume to keep receiving events from the engine."
              : "Insert a row or open a page — every storage operation the engine performs appears here."
          }
        />
      ) : (
        <ul className="divide-y divide-[var(--border-subtle)] font-mono text-[11px]">
          {visible.map((event) => (
            <li key={event.seq} className="flex gap-2 px-3 py-1 hover:bg-[var(--surface-sunken)]">
              <span className="text-muted w-10 shrink-0 text-right">#{event.seq}</span>
              <span className="text-muted w-20 shrink-0">
                {formatTimestamp(event.timestamp_ns)}
              </span>
              <span
                className={cn(
                  "w-16 shrink-0 truncate rounded px-1 text-center text-[10px]",
                  CATEGORY_TONE[event.category] ?? "bg-zinc-500/15",
                )}
              >
                {event.category}
              </span>
              <span className="w-44 shrink-0 truncate font-sans font-medium">
                {event.event_type.replace(/Event$/, "")}
              </span>
              <span className="text-muted min-w-0 flex-1 truncate">
                {summarize(event.event)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
