/**
 * The page inspector: header fields, slot directory, decoded records, raw hex.
 *
 * Everything shown comes from `GET /pages/{id}` — real bytes read from the real
 * file. Nothing here is reconstructed client-side.
 */

import { useEffect, useState } from "react";
import {
  Badge,
  EmptyState,
  ErrorNotice,
  Field,
  Panel,
  Spinner,
  toneForPageType,
} from "@/components/primitives";
import { usePage } from "@/hooks/useEngine";
import { cn, formatBytes, formatHex, formatValue } from "@/lib/format";
import { HexView } from "./HexView";
import { PageLayoutBar } from "./PageLayoutBar";
import type { PageDetailModel, SlotDetailModel } from "@/types/api";

type Tab = "layout" | "header" | "slots" | "hex";

const TABS: { id: Tab; label: string }[] = [
  { id: "layout", label: "Layout" },
  { id: "header", label: "Header" },
  { id: "slots", label: "Slots" },
  { id: "hex", label: "Hex" },
];

export function PageInspector({
  databaseId,
  pageId,
}: {
  databaseId: string | null;
  pageId: number | null;
}) {
  const [tab, setTab] = useState<Tab>("layout");
  const [selectedSlotId, setSelectedSlotId] = useState<number | null>(null);
  const query = usePage(databaseId, pageId);

  // Selecting a different page must not keep a stale slot selection, which
  // would highlight unrelated bytes in the hex view.
  useEffect(() => setSelectedSlotId(null), [pageId]);

  const body = () => {
    if (pageId === null) {
      return (
        <EmptyState
          title="No page selected"
          hint="Choose a page from the disk map to inspect its header, slot directory and raw bytes."
        />
      );
    }
    if (query.isPending) return <Spinner label="Reading page" />;
    if (query.isError) {
      return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />;
    }

    const page = query.data;
    return (
      <div className="flex min-h-0 flex-col">
        <PageHeaderSummary page={page} />
        <TabBar tab={tab} onChange={setTab} />
        <div className="scroll-thin min-h-0 flex-1 overflow-auto">
          {tab === "layout" ? (
            <PageLayoutBar
              page={page}
              selectedSlotId={selectedSlotId}
              onSelectSlot={setSelectedSlotId}
            />
          ) : null}
          {tab === "header" ? <HeaderFields page={page} /> : null}
          {tab === "slots" ? (
            <SlotList
              page={page}
              selectedSlotId={selectedSlotId}
              onSelectSlot={setSelectedSlotId}
            />
          ) : null}
          {tab === "hex" ? (
            <HexView page={page} selectedSlotId={selectedSlotId} />
          ) : null}
        </div>
      </div>
    );
  };

  return (
    <Panel
      title="Page inspector"
      subtitle={pageId === null ? "select a page" : `page ${pageId}`}
      className="h-full"
      bodyClassName="flex flex-col"
    >
      {body()}
    </Panel>
  );
}

function PageHeaderSummary({ page }: { page: PageDetailModel }) {
  const { summary } = page;
  return (
    <div className="shrink-0 border-b border-[var(--border-subtle)] p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge tone={toneForPageType(summary.page_type)}>{summary.page_type}</Badge>
        <span className="font-mono text-sm font-semibold">page {summary.page_id}</span>
        <Badge tone={summary.checksum_valid ? "heap" : "danger"}>
          {summary.checksum_valid ? "checksum ok" : "CHECKSUM FAILED"}
        </Badge>
        {summary.owner ? <Badge tone="neutral">{summary.owner}</Badge> : null}
      </div>
      {summary.error ? (
        <p className="mb-2 rounded bg-red-500/10 p-2 text-[11px] text-red-600 dark:text-red-300">
          {summary.error}
        </p>
      ) : null}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
        <Field label="file offset" value={summary.file_offset} />
        <Field label="slots" value={`${summary.slot_count} (${summary.live_record_count} live)`} />
        <Field label="free" value={formatBytes(summary.free_space)} />
        <Field
          label="reclaimable"
          value={formatBytes(summary.reclaimable_space)}
          title="Space held by tombstoned records. Not contiguous, so it cannot satisfy an insert until the page is compacted."
        />
        <Field label="checksum" value={formatHex(summary.checksum)} />
        <Field
          label="lsn"
          value={summary.lsn}
          title="Log sequence number: the log record that last described this page. Recovery replays anything whose LSN is newer than this, and skips the rest."
        />
        <Field label="next page" value={summary.next_page_id ?? "—"} />
        <Field label="page size" value={formatBytes(page.page_size)} />
      </dl>
    </div>
  );
}

function TabBar({ tab, onChange }: { tab: Tab; onChange: (next: Tab) => void }) {
  return (
    <div
      role="tablist"
      aria-label="Page inspector views"
      className="flex shrink-0 gap-1 border-b border-[var(--border-subtle)] px-2 py-1.5"
    >
      {TABS.map((entry) => (
        <button
          key={entry.id}
          role="tab"
          type="button"
          aria-selected={tab === entry.id}
          onClick={() => onChange(entry.id)}
          className={cn(
            "rounded px-2 py-1 text-xs font-medium transition-colors",
            tab === entry.id
              ? "bg-[var(--accent)] text-white"
              : "hover:bg-[var(--surface-sunken)]",
          )}
        >
          {entry.label}
        </button>
      ))}
    </div>
  );
}

function HeaderFields({ page }: { page: PageDetailModel }) {
  return (
    <table className="w-full text-left font-mono text-[11px]">
      <thead className="surface-sunken text-muted sticky top-0">
        <tr>
          <th className="px-3 py-1.5 font-medium">field</th>
          <th className="px-3 py-1.5 font-medium">@</th>
          <th className="px-3 py-1.5 font-medium">size</th>
          <th className="px-3 py-1.5 font-medium">value</th>
          <th className="px-3 py-1.5 font-medium">bytes</th>
        </tr>
      </thead>
      <tbody>
        {page.header_fields.map((field) => (
          <tr
            key={field.name}
            className="border-t border-[var(--border-subtle)] align-top"
            title={field.description}
          >
            <td className="px-3 py-1.5 font-sans font-medium">{field.name}</td>
            <td className="text-muted px-3 py-1.5">{field.offset}</td>
            <td className="text-muted px-3 py-1.5">{field.size}B</td>
            <td className="px-3 py-1.5">{String(field.value)}</td>
            <td className="text-muted px-3 py-1.5 break-all">{field.raw_hex}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SlotList({
  page,
  selectedSlotId,
  onSelectSlot,
}: {
  page: PageDetailModel;
  selectedSlotId: number | null;
  onSelectSlot: (slotId: number | null) => void;
}) {
  if (page.slots.length === 0) {
    return (
      <EmptyState
        title="No slot directory"
        hint={
          page.summary.page_type === "META"
            ? "The meta page has a fixed 60-byte header instead of a slot directory. See the Header tab."
            : "This page holds no records yet."
        }
      />
    );
  }

  return (
    <ul className="divide-y divide-[var(--border-subtle)]">
      {page.slots.map((slot) => (
        <SlotRow
          key={slot.slot_id}
          slot={slot}
          selected={selectedSlotId === slot.slot_id}
          onSelect={() =>
            onSelectSlot(selectedSlotId === slot.slot_id ? null : slot.slot_id)
          }
        />
      ))}
    </ul>
  );
}

function SlotRow({
  slot,
  selected,
  onSelect,
}: {
  slot: SlotDetailModel;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li className={cn(selected && "bg-[var(--accent)]/8")}>
      <button
        type="button"
        onClick={onSelect}
        aria-expanded={selected}
        className="w-full px-3 py-2 text-left hover:bg-[var(--surface-sunken)]"
      >
        <div className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
          <span className="font-semibold">slot {slot.slot_id}</span>
          {slot.is_live ? (
            <>
              <span className="text-muted">offset {slot.offset}</span>
              <span className="text-muted">len {slot.length}</span>
            </>
          ) : (
            <Badge
              tone="danger"
              title="Tombstone: the slot entry is (0,0). The bytes stay until the page is compacted, and the slot id is never reused by a different row unless it is reclaimed."
            >
              deleted
            </Badge>
          )}
        </div>

        {slot.record ? (
          <div className="mt-1.5 space-y-1">
            <div className="flex flex-wrap gap-1">
              {slot.record.fields.map((field) => (
                <span
                  key={field.index}
                  title={
                    field.is_null
                      ? `${field.name}: NULL, flagged in the bitmap, occupies no bytes`
                      : `${field.name}: bytes ${field.offset}–${field.offset + field.length}`
                  }
                  className={cn(
                    "rounded px-1.5 py-0.5 font-mono text-[10px]",
                    field.is_null
                      ? "bg-zinc-500/15 text-[var(--text-secondary)] italic"
                      : "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
                  )}
                >
                  <span className="opacity-60">{field.name}=</span>
                  {formatValue(field.value)}
                </span>
              ))}
            </div>
            <p className="text-muted font-mono text-[10px]">
              null bitmap 0x{slot.record.null_bitmap_hex} ={" "}
              {slot.record.null_bitmap_bits.map((bit) => (bit ? "1" : "0")).join("")}
              <span className="ml-2">· {slot.record.total_size} bytes total</span>
            </p>
          </div>
        ) : null}

        {slot.decode_error ? (
          <p className="mt-1 text-[10px] text-red-600 dark:text-red-300">
            decode failed: {slot.decode_error}
          </p>
        ) : null}

        {selected && slot.is_live ? (
          <pre className="surface-sunken text-muted mt-2 overflow-x-auto rounded p-2 font-mono text-[10px] break-all whitespace-pre-wrap">
            {slot.raw_hex || "(empty)"}
          </pre>
        ) : null}
      </button>
    </li>
  );
}
