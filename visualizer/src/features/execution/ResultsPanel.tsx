/**
 * Query results, with the cost of producing them.
 *
 * The footer is the point: rows scanned versus rows returned is what makes a
 * missing index visible, and it is the number Milestone 5 and 7 will improve.
 */

import { Badge, EmptyState, Panel } from "@/components/primitives";
import { cn, formatCount, formatDuration, formatValue } from "@/lib/format";
import type { PlanModel, QueryResultModel } from "@/types/api";

/**
 * How the chosen plan reads its tables, `seq scan`, `index scan`, or both.
 *
 * Read off the plan rather than assumed. This badge was the literal string
 * "seq scan" from Milestone 3 until Milestone 16, so every index scan the
 * planner chose from Milestone 5 onward was mislabelled, and every join from
 * Milestone 13 was reported by only one of its two sides.
 */
function accessPath(plan: PlanModel | null | undefined): string | null {
  if (!plan) return null;
  const scans = plan.nodes
    .map((node) => node.operator_type)
    .filter((type) => type.endsWith("Scan"));
  if (scans.length === 0) return null;
  const kinds = [...new Set(scans)].map((type) =>
    type === "IndexScan" ? "index scan" : "seq scan",
  );
  // A join can read one side each way, and saying so is the interesting part.
  return [...new Set(kinds)].sort().join(" + ");
}

export function ResultsPanel({
  results,
  isPending,
  error,
  onSelectPage,
}: {
  results: QueryResultModel[] | undefined;
  isPending: boolean;
  error: unknown;
  onSelectPage?: (pageId: number) => void;
}) {
  const last = results?.at(-1);

  const subtitle = () => {
    if (isPending) return "running…";
    if (!results) return undefined;
    if (results.length > 1) return `${results.length} statements`;
    return last?.returns_rows
      ? `${formatCount(last.rows_returned)} row(s)`
      : last?.message;
  };

  return (
    <Panel title="Results" subtitle={subtitle()} className="h-full" bodyClassName="flex flex-col">
      {error ? (
        <QueryError error={error} />
      ) : !results ? (
        <EmptyState
          title="Nothing run yet"
          hint="Write a statement and press ⌘↵. A script runs every statement and reports each one."
        />
      ) : (
        <div className="scroll-thin min-h-0 flex-1 overflow-auto">
          {results.map((result, index) => (
            <StatementResult
              key={index}
              result={result}
              index={index}
              showHeading={results.length > 1}
              onSelectPage={onSelectPage}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}

function QueryError({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String((error as { code: unknown }).code)
      : "Error";
  return (
    <div role="alert" className="p-3">
      <p className="mb-1 font-mono text-[11px] font-semibold text-red-600 dark:text-red-400">
        {code}
      </p>
      <p className="text-[11px] text-red-600 dark:text-red-400">{message}</p>
    </div>
  );
}

function StatementResult({
  result,
  index,
  showHeading,
  onSelectPage,
}: {
  result: QueryResultModel;
  index: number;
  showHeading: boolean;
  onSelectPage?: (pageId: number) => void;
}) {
  return (
    <section className="border-b border-[var(--border-subtle)] last:border-b-0">
      {showHeading ? (
        <header className="surface-sunken flex items-baseline gap-2 px-3 py-1">
          <span className="text-muted font-mono text-[10px]">{index + 1}</span>
          <span className="font-mono text-[11px] font-semibold">
            {result.statement_kind.replace("Statement", "")}
          </span>
          {result.message ? (
            <span className="text-muted truncate text-[11px]">{result.message}</span>
          ) : null}
        </header>
      ) : null}

      {result.returns_rows ? (
        <RowTable result={result} onSelectPage={onSelectPage} />
      ) : (
        <p className="text-muted px-3 py-2 text-[11px]">
          {result.message || "done"}
          {result.rows_affected > 0
            ? ` · ${formatCount(result.rows_affected)} row(s) affected`
            : ""}
        </p>
      )}

      <CostFooter result={result} />
    </section>
  );
}

function RowTable({
  result,
  onSelectPage,
}: {
  result: QueryResultModel;
  onSelectPage?: (pageId: number) => void;
}) {
  if (result.rows.length === 0) {
    return (
      <p className="text-muted px-3 py-3 text-[11px]">
        No rows matched.{" "}
        {result.rows_scanned > 0
          ? `${formatCount(result.rows_scanned)} scanned, all filtered out. Remember a NULL comparison is unknown, not true.`
          : "The table is empty."}
      </p>
    );
  }

  const hasRecordIds = result.record_ids.length === result.rows.length;

  return (
    <table className="w-full border-collapse text-left text-xs">
      <thead className="surface-sunken sticky top-0 z-10">
        <tr className="border-b border-[var(--border-subtle)]">
          <th className="text-muted px-2 py-1 font-medium">#</th>
          {hasRecordIds ? (
            <th
              className="text-muted px-2 py-1 font-medium"
              title="Physical address: (page, slot). PostgreSQL calls this a ctid."
            >
              rid
            </th>
          ) : null}
          {result.columns.map((column) => (
            <th key={column.name} className="px-2 py-1 font-medium">
              {column.name}
              <span className="text-muted ml-1.5 font-mono text-[10px] font-normal">
                {column.type ?? "?"}
              </span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {result.rows.map((row, rowIndex) => (
          <tr
            key={rowIndex}
            className="border-b border-[var(--border-subtle)] hover:bg-[var(--surface-sunken)]"
          >
            <td className="text-muted px-2 py-1 font-mono text-[10px]">
              {rowIndex + 1}
            </td>
            {hasRecordIds ? (
              <td className="px-2 py-1">
                <button
                  type="button"
                  onClick={() =>
                    onSelectPage?.(result.record_ids[rowIndex]!.page_id)
                  }
                  title={`Inspect page ${result.record_ids[rowIndex]!.page_id}`}
                  className="font-mono text-[10px] text-[var(--accent)] hover:underline"
                >
                  ({result.record_ids[rowIndex]!.page_id},
                  {result.record_ids[rowIndex]!.slot_id})
                </button>
              </td>
            ) : null}
            {row.map((value, columnIndex) => (
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
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CostFooter({ result }: { result: QueryResultModel }) {
  return (
    <footer className="text-muted flex flex-wrap items-center gap-3 px-3 py-1.5 font-mono text-[10px]">
      {result.returns_rows ? (
        <>
          <span title="Rows the scan produced before any filtering">
            scanned {formatCount(result.rows_scanned)}
          </span>
          <span title="Rows a filter dropped because the predicate was not exactly TRUE">
            rejected {formatCount(result.rows_rejected)}
          </span>
          <span>returned {formatCount(result.rows_returned)}</span>
        </>
      ) : (
        <span>affected {formatCount(result.rows_affected)}</span>
      )}
      <span title="Page reads and writes this statement caused. A read served from the buffer pool costs about a third of a miss.">
        pages {formatCount(result.pages_read)}r
        {result.pages_written > 0 ? ` ${formatCount(result.pages_written)}w` : ""}
      </span>
      <span>{formatDuration(result.duration_ns)}</span>
      {result.truncated ? (
        <Badge tone="danger" title="The row ceiling cut this result short">
          truncated
        </Badge>
      ) : null}
      {result.cancelled ? <Badge tone="neutral">cancelled</Badge> : null}
      {accessPath(result.plan) ? (
        <Badge
          tone="neutral"
          title="The access path the planner actually chose. An index scan needs an index covering the predicate and a cost model that believes it is cheaper. EXPLAIN shows what it rejected."
        >
          {accessPath(result.plan)}
        </Badge>
      ) : null}
    </footer>
  );
}
