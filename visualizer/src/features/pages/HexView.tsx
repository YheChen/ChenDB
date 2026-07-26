/**
 * A hexdump with the page's regions colour-coded.
 *
 * The point is not to show hex — it is to make the *structure* visible: which
 * bytes are the header, which are the slot directory, which are the record a
 * slot points at. Selecting a slot highlights exactly its bytes, so the link
 * between "slot 1 says offset 4039, length 24" and the actual bytes at 4039 is
 * something you can see rather than something you take on trust.
 */

import { useMemo } from "react";
import { cn, hexBytes } from "@/lib/format";
import type { PageDetailModel } from "@/types/api";

const BYTES_PER_ROW = 16;
/** Rendering 4096 bytes as spans is fine; 64 KiB pages are not. */
const MAX_BYTES_RENDERED = 8192;

type ByteKind = "header" | "slots" | "free" | "records" | "selected";

const KIND_CLASS: Record<ByteKind, string> = {
  header: "text-violet-600 dark:text-violet-300",
  slots: "text-sky-600 dark:text-sky-300",
  free: "text-[var(--text-secondary)] opacity-40",
  records: "text-emerald-700 dark:text-emerald-300",
  selected: "bg-[var(--accent)] text-white rounded-xs",
};

function classify(
  offset: number,
  page: PageDetailModel,
  selection: { start: number; end: number } | null,
): ByteKind {
  if (selection && offset >= selection.start && offset < selection.end) {
    return "selected";
  }
  if (offset < page.header_size) return "header";
  if (offset < page.slot_directory_end) return "slots";
  if (offset < page.free_end) return "free";
  return "records";
}

export function HexView({
  page,
  selectedSlotId,
}: {
  page: PageDetailModel;
  selectedSlotId: number | null;
}) {
  const bytes = useMemo(() => hexBytes(page.raw_hex), [page.raw_hex]);

  const selection = useMemo(() => {
    if (selectedSlotId === null) return null;
    const slot = page.slots.find((entry) => entry.slot_id === selectedSlotId);
    if (!slot || !slot.is_live) return null;
    return { start: slot.offset, end: slot.offset + slot.length };
  }, [page.slots, selectedSlotId]);

  const truncated = bytes.length > MAX_BYTES_RENDERED;
  const visible = truncated ? bytes.slice(0, MAX_BYTES_RENDERED) : bytes;

  const rows: number[] = [];
  for (let start = 0; start < visible.length; start += BYTES_PER_ROW) {
    rows.push(start);
  }

  return (
    <div className="p-3 font-mono text-[11px] leading-5">
      <div className="text-muted mb-2 flex flex-wrap gap-3 text-[10px]">
        <Legend className="text-violet-600 dark:text-violet-300" label="header" />
        <Legend className="text-sky-600 dark:text-sky-300" label="slot directory" />
        <Legend className="opacity-40" label="free space" />
        <Legend className="text-emerald-700 dark:text-emerald-300" label="records" />
        {selection ? (
          <Legend
            className="bg-[var(--accent)] px-1 text-white"
            label={`slot ${selectedSlotId}`}
          />
        ) : null}
      </div>

      {rows.map((rowStart) => (
        <div key={rowStart} className="flex gap-3 whitespace-pre">
          <span className="text-muted select-none">
            {(page.summary.file_offset + rowStart).toString(16).padStart(8, "0")}
          </span>
          <span>
            {visible.slice(rowStart, rowStart + BYTES_PER_ROW).map((pair, index) => {
              const offset = rowStart + index;
              return (
                <span key={offset} className={KIND_CLASS[classify(offset, page, selection)]}>
                  {pair}
                  {index === BYTES_PER_ROW - 1 ? "" : " "}
                </span>
              );
            })}
          </span>
          <span className="text-muted">
            |
            {visible
              .slice(rowStart, rowStart + BYTES_PER_ROW)
              .map((pair) => {
                const code = Number.parseInt(pair, 16);
                return code >= 0x20 && code < 0x7f ? String.fromCharCode(code) : ".";
              })
              .join("")}
            |
          </span>
        </div>
      ))}

      {truncated ? (
        <p className="text-muted mt-2">
          … {bytes.length - MAX_BYTES_RENDERED} more bytes not rendered
        </p>
      ) : null}
    </div>
  );
}

function Legend({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={cn("font-mono", className)}>██</span>
      {label}
    </span>
  );
}
