/**
 * The schema browser: what tables exist, and what each one costs.
 *
 * Everything shown is read out of the real system tables. The catalog's own
 * tables can be shown too — they are ordinary heaps holding ordinary rows, which
 * is the point of Milestone 4 — but they are hidden by default so the user's
 * schema comes first.
 */

import { useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNotice,
  Field,
  Panel,
  Spinner,
} from "@/components/primitives";
import { useCatalog, useTable } from "@/hooks/useEngine";
import { cn, formatBytes, formatCount, percentOf } from "@/lib/format";
import type { TableDetail, TableSummary } from "@/types/api";

export function CatalogPanel({
  databaseId,
  selectedTable,
  onSelectTable,
}: {
  databaseId: string | null;
  selectedTable: string | null;
  onSelectTable: (table: string | null) => void;
}) {
  const [showSystem, setShowSystem] = useState(false);
  const query = useCatalog(databaseId);

  const subtitle = query.data
    ? `${formatCount(query.data.tables.length)} table(s) · catalog cache ${(
        query.data.stats.hit_rate * 100
      ).toFixed(0)}% hit`
    : undefined;

  return (
    <Panel
      title="Catalog"
      subtitle={subtitle}
      className="h-full"
      actions={
        <Button
          onClick={() => setShowSystem((current) => !current)}
          aria-pressed={showSystem}
          title="The catalog's own tables are ordinary heaps holding ordinary rows"
        >
          {showSystem ? "Hide system" : "Show system"}
        </Button>
      }
    >
      {!databaseId ? (
        <EmptyState title="No database open" hint="Create or select one above." />
      ) : query.isPending ? (
        <Spinner label="Reading catalog" />
      ) : query.isError ? (
        <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.tables.length === 0 && !showSystem ? (
        <EmptyState
          title="No tables yet"
          hint="Run CREATE TABLE in the Execution workspace, or show the system tables to see the catalog itself."
        />
      ) : (
        <ul className="divide-y divide-[var(--border-subtle)]">
          {query.data.tables.map((table) => (
            <TableRow
              key={table.table_id}
              table={table}
              selected={table.name === selectedTable}
              onSelect={() =>
                onSelectTable(table.name === selectedTable ? null : table.name)
              }
            />
          ))}
          {showSystem ? (
            <>
              <li className="text-muted surface-sunken px-3 py-1 text-[10px] tracking-wide uppercase">
                system tables
              </li>
              {query.data.system_tables.map((table) => (
                <TableRow
                  key={table.table_id}
                  table={table}
                  selected={table.name === selectedTable}
                  onSelect={() =>
                    onSelectTable(table.name === selectedTable ? null : table.name)
                  }
                />
              ))}
            </>
          ) : null}
        </ul>
      )}
    </Panel>
  );
}

function TableRow({
  table,
  selected,
  onSelect,
}: {
  table: TableSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "true" : undefined}
        aria-label={`${table.name}, ${table.column_count} columns, ${table.row_count} rows`}
        className={cn(
          "w-full px-3 py-2 text-left transition-colors",
          selected ? "bg-[var(--accent)]/12" : "hover:bg-[var(--surface-sunken)]",
        )}
      >
        <div className="flex items-baseline gap-2">
          <span className="min-w-0 flex-1 truncate text-xs font-semibold">
            {table.name}
          </span>
          {table.is_system ? <Badge tone="meta">system</Badge> : null}
          <span className="text-muted font-mono text-[10px]">#{table.table_id}</span>
        </div>
        <p className="text-muted mt-0.5 font-mono text-[10px]">
          {table.column_count} col · {formatCount(table.row_count)} rows ·{" "}
          {table.page_count} page{table.page_count === 1 ? "" : "s"}
        </p>
      </button>
    </li>
  );
}

/** Schema and storage detail for one selected table. */
export function TableDetailPanel({
  databaseId,
  table,
  onSelectPage,
}: {
  databaseId: string | null;
  table: string | null;
  onSelectPage?: (pageId: number) => void;
}) {
  const query = useTable(databaseId, table);

  return (
    <Panel
      title="Table"
      subtitle={table ?? "select a table"}
      className="h-full"
    >
      {!table ? (
        <EmptyState
          title="No table selected"
          hint="Choose one from the catalog to see its columns and what it costs on disk."
        />
      ) : query.isPending ? (
        <Spinner label="Reading table" />
      ) : query.isError ? (
        <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <TableBody detail={query.data} onSelectPage={onSelectPage} />
      )}
    </Panel>
  );
}

function TableBody({
  detail,
  onSelectPage,
}: {
  detail: TableDetail;
  onSelectPage?: (pageId: number) => void;
}) {
  const { storage } = detail;
  const used = storage.bytes_allocated - storage.free_space;

  return (
    <div className="space-y-3 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">{detail.name}</span>
        {detail.is_system ? <Badge tone="meta">system</Badge> : null}
        <Badge tone="neutral">table_id {detail.table_id}</Badge>
      </div>

      <table className="w-full text-left text-xs">
        <thead className="surface-sunken text-muted">
          <tr>
            <th className="px-2 py-1 font-medium">#</th>
            <th className="px-2 py-1 font-medium">column</th>
            <th className="px-2 py-1 font-medium">type</th>
            <th className="px-2 py-1 font-medium">width</th>
            <th className="px-2 py-1 font-medium">constraints</th>
          </tr>
        </thead>
        <tbody>
          {detail.columns.map((column, index) => (
            <tr key={column.name} className="border-t border-[var(--border-subtle)]">
              <td
                className="text-muted px-2 py-1 font-mono text-[10px]"
                title="Position in the record layout — the link between the catalog and the on-disk row"
              >
                {index}
              </td>
              <td className="px-2 py-1 font-medium">{column.name}</td>
              <td className="px-2 py-1 font-mono text-[11px]">{column.type}</td>
              <td className="text-muted px-2 py-1 font-mono text-[11px]">
                {column.fixed_size === null ? "var" : `${column.fixed_size} B`}
              </td>
              <td className="px-2 py-1">
                <span className="flex gap-1">
                  {column.primary_key ? <Badge tone="accent">PK</Badge> : null}
                  {!column.nullable ? <Badge tone="neutral">NOT NULL</Badge> : null}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div>
        <p className="text-muted mb-1 text-[10px] tracking-wide uppercase">storage</p>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
          <Field
            label="rows"
            value={formatCount(storage.row_count)}
            title="What a SELECT would return. O(pages) to compute — nothing is cached, the same reason PostgreSQL's reltuples is only an estimate."
          />
          <Field
            label="versions"
            value={formatCount(storage.version_count)}
            title="Row versions physically on the pages. Higher than 'rows' after a delete or an update; the difference is what Vacuum reclaims."
          />
          <Field label="pages" value={storage.page_count} />
          <Field label="allocated" value={formatBytes(storage.bytes_allocated)} />
          <Field label="used" value={formatBytes(used)} />
          <Field label="free" value={formatBytes(storage.free_space)} />
          <Field
            label="reclaimable"
            value={formatBytes(storage.reclaimable_space)}
            title="Bytes held by tombstoned rows. Not contiguous, so compaction is needed to reuse them."
          />
        </dl>

        <div
          className="surface-sunken mt-2 h-2 overflow-hidden rounded-full"
          title={`${formatBytes(used)} of ${formatBytes(storage.bytes_allocated)} used`}
        >
          <div
            className="h-full rounded-full bg-emerald-500"
            style={{ width: `${percentOf(used, storage.bytes_allocated)}%` }}
          />
        </div>
      </div>

      <div>
        <p className="text-muted mb-1 text-[10px] tracking-wide uppercase">
          heap chain · click to inspect
        </p>
        <div className="flex flex-wrap gap-1">
          {storage.page_ids.map((pageId) => (
            <button
              key={pageId}
              type="button"
              onClick={() => onSelectPage?.(pageId)}
              title={`Inspect page ${pageId}`}
              className="surface-sunken rounded border border-[var(--border-subtle)] px-1.5 py-0.5 font-mono text-[10px] hover:border-[var(--accent)]"
            >
              {pageId}
            </button>
          ))}
        </div>
        <p className="text-muted mt-1 font-mono text-[10px]">
          first {storage.first_page} → last {storage.last_page}
        </p>
      </div>
    </div>
  );
}
