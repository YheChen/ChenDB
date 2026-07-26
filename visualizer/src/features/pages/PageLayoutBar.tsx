/**
 * The slotted-page layout, drawn to scale.
 *
 * A page is four regions that tile it exactly:
 *
 *     ├── header ──┼── slot directory ──┼── free space ──┼── records ──┤
 *     0            24        free_start           free_end     page_size
 *
 * Widths are real byte proportions, not decoration, so a nearly-full page looks
 * full and the free-space region visibly shrinks as rows are inserted.
 */

import { Fragment } from "react";
import { cn, formatBytes, percentOf } from "@/lib/format";
import type { PageDetailModel } from "@/types/api";

interface Region {
  key: string;
  label: string;
  start: number;
  end: number;
  className: string;
  description: string;
}

function regionsOf(page: PageDetailModel): Region[] {
  return [
    {
      key: "header",
      label: "Header",
      start: 0,
      end: page.header_size,
      className: "bg-violet-500/70",
      description: "Checksum, LSN, page type, free-space pointers, next page id",
    },
    {
      key: "slots",
      label: "Slot directory",
      start: page.header_size,
      end: page.slot_directory_end,
      className: "bg-sky-500/70",
      description: "4 bytes per slot: (offset, length). Grows forward.",
    },
    {
      key: "free",
      label: "Free space",
      start: page.free_start,
      end: page.free_end,
      className: "bg-zinc-400/30 dark:bg-zinc-500/30",
      description: "The gap the two regions grow into. An insert needs room here.",
    },
    {
      key: "records",
      label: "Record data",
      start: page.free_end,
      end: page.page_size,
      className: "bg-emerald-500/70",
      description: "Tuple bytes, written backwards from the end of the page.",
    },
  ];
}

export function PageLayoutBar({
  page,
  selectedSlotId,
  onSelectSlot,
}: {
  page: PageDetailModel;
  selectedSlotId: number | null;
  onSelectSlot: (slotId: number | null) => void;
}) {
  const regions = regionsOf(page);
  const liveSlots = page.slots.filter((slot) => slot.is_live);

  return (
    <div className="space-y-3 p-3">
      <div
        className="flex h-9 w-full overflow-hidden rounded border border-[var(--border-subtle)]"
        role="img"
        aria-label={`Page ${page.summary.page_id} layout, ${page.page_size} bytes`}
      >
        {regions.map((region) => {
          const width = percentOf(region.end - region.start, page.page_size);
          if (width <= 0) return null;
          return (
            <div
              key={region.key}
              className={cn("relative min-w-px", region.className)}
              style={{ width: `${width}%` }}
              title={`${region.label}: bytes ${region.start}–${region.end} (${formatBytes(region.end - region.start)})\n${region.description}`}
            />
          );
        })}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
        {regions.map((region) => (
          <Fragment key={region.key}>
            <div className="min-w-0">
              <dt className="flex items-center gap-1.5">
                <span className={cn("size-2 shrink-0 rounded-sm", region.className)} />
                <span className="text-muted truncate text-[10px] tracking-wide uppercase">
                  {region.label}
                </span>
              </dt>
              <dd className="pl-3.5 font-mono text-[11px]">
                [{region.start}, {region.end}){" "}
                <span className="text-muted">
                  {formatBytes(region.end - region.start)}
                </span>
              </dd>
            </div>
          </Fragment>
        ))}
      </dl>

      {liveSlots.length > 0 ? (
        <div>
          <p className="text-muted mb-1 text-[10px] tracking-wide uppercase">
            Records within the data region · click to inspect
          </p>
          <div className="flex h-6 w-full flex-row-reverse overflow-hidden rounded border border-[var(--border-subtle)]">
            {/* Reversed: slot 0 sits highest in the page, so laying the row out
                right-to-left matches the physical byte order. */}
            {liveSlots.map((slot) => (
              <button
                key={slot.slot_id}
                type="button"
                onClick={() =>
                  onSelectSlot(selectedSlotId === slot.slot_id ? null : slot.slot_id)
                }
                title={`Slot ${slot.slot_id}: offset ${slot.offset}, ${slot.length} bytes`}
                aria-pressed={selectedSlotId === slot.slot_id}
                className={cn(
                  "min-w-[2px] border-l border-black/10 font-mono text-[9px] transition-colors dark:border-white/10",
                  selectedSlotId === slot.slot_id
                    ? "bg-[var(--accent)] text-white"
                    : "bg-emerald-500/60 hover:bg-emerald-500/80",
                )}
                style={{
                  width: `${percentOf(slot.length, page.page_size - page.free_end)}%`,
                }}
              >
                {slot.slot_id}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
