/**
 * Query results, plus the cost of producing them.
 *
 * Milestone 1 has no SQL, so this is a full heap scan with an offset window.
 * The footer reports rows scanned, pages read and elapsed time, the same
 * numbers a `SELECT` will report once the executor exists, and the numbers the
 * buffer pool and index milestones will visibly improve.
 */

import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNotice,
  Panel,
  Spinner,
} from "@/components/primitives";
import { useDeleteRecord, useRecords } from "@/hooks/useEngine";
import { cn, formatCount, formatDuration, formatValue } from "@/lib/format";

const PAGE_SIZE_OPTIONS = [25, 50, 100, 250];

export function RecordsPanel({
  databaseId,
  table,
  onSelectPage,
}: {
  databaseId: string | null;
  table: string | null;
  onSelectPage: (pageId: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(50);
  const query = useRecords(databaseId, table, offset, limit);
  const remove = useDeleteRecord(databaseId ?? "", table ?? "");

  // A different table means a different row set; keeping the offset would show
  // page 3 of a table that has one page.
  useEffect(() => setOffset(0), [table]);

  if (!databaseId) {
    return (
      <Panel title="Rows" className="h-full">
        <EmptyState title="No database open" />
      </Panel>
    );
  }

  if (!table) {
    return (
      <Panel title="Rows" className="h-full">
        <EmptyState
          title="No table selected"
          hint="Pick one from the catalog, or run CREATE TABLE in the Execution workspace."
        />
      </Panel>
    );
  }

  return (
    <Panel
      title={`Rows · ${table}`}
      subtitle={
        query.data
          ? `${formatCount(query.data.returned)} shown from offset ${query.data.offset}`
          : undefined
      }
      className="h-full"
      bodyClassName="flex flex-col"
      actions={
        <>
          <select
            aria-label="Rows per page"
            value={limit}
            onChange={(event) => {
              setLimit(Number(event.target.value));
              setOffset(0);
            }}
            className="surface-sunken rounded border border-[var(--border-subtle)] px-1.5 py-0.5 text-[11px]"
          >
            {PAGE_SIZE_OPTIONS.map((size) => (
              <option key={size} value={size}>
                {size} rows
              </option>
            ))}
          </select>
          <Button
            onClick={() => setOffset((current) => Math.max(0, current - limit))}
            disabled={offset === 0}
          >
            ← Prev
          </Button>
          <Button
            onClick={() => setOffset((current) => current + limit)}
            disabled={!query.data?.has_more}
          >
            Next →
          </Button>
        </>
      }
    >
      {query.isPending ? (
        <Spinner label="Scanning" />
      ) : query.isError ? (
        <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.rows.length === 0 ? (
        <EmptyState
          title="No rows"
          hint="Insert one from the Schema panel to see it land in a heap page."
        />
      ) : (
        <>
          <div className="scroll-thin min-h-0 flex-1 overflow-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead className="surface-sunken sticky top-0 z-10">
                <tr className="border-b border-[var(--border-subtle)]">
                  <th className="text-muted px-2 py-1.5 font-medium">#</th>
                  <th
                    className="text-muted px-2 py-1.5 font-medium"
                    title="Physical address: (page, slot). PostgreSQL calls this a ctid."
                  >
                    rid
                  </th>
                  {query.data.columns.map((column) => (
                    <th key={column.name} className="px-2 py-1.5 font-medium">
                      <span>{column.name}</span>
                      <span className="text-muted ml-1.5 font-mono text-[10px] font-normal">
                        {column.type}
                        {column.primary_key ? " PK" : ""}
                        {!column.nullable ? " ¬∅" : ""}
                      </span>
                    </th>
                  ))}
                  <th className="w-8" />
                </tr>
              </thead>
              <tbody>
                {query.data.rows.map((row, index) => (
                  <tr
                    key={`${row.record_id.page_id}-${row.record_id.slot_id}`}
                    className="border-b border-[var(--border-subtle)] hover:bg-[var(--surface-sunken)]"
                  >
                    <td className="text-muted px-2 py-1 font-mono text-[10px]">
                      {offset + index + 1}
                    </td>
                    <td className="px-2 py-1">
                      <button
                        type="button"
                        onClick={() => onSelectPage(row.record_id.page_id)}
                        title={`Inspect page ${row.record_id.page_id}`}
                        className="font-mono text-[10px] text-[var(--accent)] hover:underline"
                      >
                        ({row.record_id.page_id},{row.record_id.slot_id})
                      </button>
                    </td>
                    {row.values.map((value, columnIndex) => (
                      <td
                        key={columnIndex}
                        className={cn(
                          "px-2 py-1 font-mono",
                          value === null && "text-muted italic",
                        )}
                      >
                        {formatValue(value)}
                      </td>
                    ))}
                    <td className="px-1 py-1">
                      <button
                        type="button"
                        aria-label={`Delete row at page ${row.record_id.page_id} slot ${row.record_id.slot_id}`}
                        onClick={() =>
                          remove.mutate({
                            pageId: row.record_id.page_id,
                            slotId: row.record_id.slot_id,
                          })
                        }
                        className="text-muted px-1 hover:text-red-600"
                        title="Tombstone this row"
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <footer className="text-muted flex shrink-0 flex-wrap items-center gap-3 border-t border-[var(--border-subtle)] px-3 py-1.5 font-mono text-[10px]">
            <span title="Rows the heap scan touched, including any skipped by the offset">
              scanned {formatCount(query.data.rows_scanned)}
            </span>
            <span>returned {formatCount(query.data.returned)}</span>
            <span title="Page reads this request caused. A hit served from the buffer pool costs about a third of a miss; the Buffer pool workspace shows which.">
              pages read {formatCount(query.data.pages_read)}
            </span>
            <span>{formatDuration(query.data.duration_ns)}</span>
            <Badge
              tone="neutral"
              title="This panel always reads the heap in physical order. An index scan is chosen by the planner, which only sees SQL. Run a SELECT in the Execution workspace to see one."
            >
              seq scan
            </Badge>
          </footer>
        </>
      )}
    </Panel>
  );
}
