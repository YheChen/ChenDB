/**
 * The undo log, drawn as what it is: a stack of page snapshots.
 *
 *   ┌──────────────────────────────────────────────┐
 *   │ #3  page 12   512 B   index split            │  ← restored first
 *   │ #2  page  7   512 B   insert                 │
 *   │ #1  page  0   512 B   allocate               │  ← restored last
 *   └──────────────────────────────────────────────┘
 *
 * Newest at the top, because that is the order a rollback replays them in. The
 * order does not actually matter here — first-write-wins means there is exactly
 * one image per page, so replaying them in any order lands in the same place —
 * but a log that displayed oldest-first would teach the wrong intuition about
 * undo in general, where order is load-bearing.
 *
 * Each row is one page's *entire contents* before the transaction touched it,
 * which is the honest way to show what "being able to change your mind" costs.
 */

import { Badge, EmptyState } from "@/components/primitives";
import { formatBytes } from "@/lib/format";
import type { UndoRecordModel } from "@/types/api";

/** Rows past this are summarised. A long transaction can hold thousands. */
const MAX_ROWS = 200;

export function UndoLogPanel({
  records,
  pageSize,
  onSelectPage,
}: {
  records: UndoRecordModel[];
  pageSize: number;
  onSelectPage?: (pageId: number) => void;
}) {
  if (records.length === 0) {
    return (
      <EmptyState
        title="Nothing to undo"
        hint="The undo log fills as the transaction writes. A page is captured once, the first time it changes."
      />
    );
  }

  const newestFirst = [...records].reverse();
  const drawn = newestFirst.slice(0, MAX_ROWS);
  const hidden = newestFirst.length - drawn.length;

  return (
    <div className="min-h-0 overflow-auto">
      <table className="w-full border-collapse font-mono text-[11px]">
        <thead className="bg-[var(--surface-sunken)] sticky top-0">
          <tr className="text-muted text-left">
            <th className="px-2 py-1 font-medium">#</th>
            <th className="px-2 py-1 font-medium">Page</th>
            <th className="px-2 py-1 font-medium">Before-image</th>
            <th className="px-2 py-1 font-medium">Captured by</th>
          </tr>
        </thead>
        <tbody>
          {drawn.map((record, position) => (
            <tr
              key={record.sequence}
              className="border-t border-[var(--border-subtle)]"
            >
              <td className="text-muted px-2 py-1">
                {record.sequence + 1}
                {position === 0 ? (
                  <span className="text-muted ml-1 text-[9px]">↩ first</span>
                ) : null}
              </td>
              <td className="px-2 py-1">
                {onSelectPage ? (
                  <button
                    type="button"
                    onClick={() => onSelectPage(record.page_id)}
                    className="text-[var(--accent)] hover:underline"
                    title="Open this page in the inspector"
                  >
                    page {record.page_id}
                  </button>
                ) : (
                  `page ${record.page_id}`
                )}
              </td>
              <td className="px-2 py-1">
                {formatBytes(record.before_image_size)}
              </td>
              <td className="px-2 py-1">
                <Badge tone="neutral">{record.reason || "write"}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {hidden > 0 ? (
        <p className="text-muted px-2 py-1 text-[10px]">
          …and {hidden} older before-images, {formatBytes(hidden * pageSize)} of
          them.
        </p>
      ) : null}
    </div>
  );
}
