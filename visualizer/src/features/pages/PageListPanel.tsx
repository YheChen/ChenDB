/**
 * The disk map: every page in the file, in file order.
 *
 * This is the view that makes "a database is a file of fixed-size pages"
 * concrete, page 0 is the header, then the schema, then the table's heap
 * chain, each at a byte offset you can point at.
 */

import {
  Badge,
  EmptyState,
  ErrorNotice,
  Panel,
  Spinner,
  toneForPageType,
} from "@/components/primitives";
import { usePages } from "@/hooks/useEngine";
import { cn, formatBytes, formatCount, percentOf } from "@/lib/format";
import type { PageSummaryModel } from "@/types/api";

export function PageListPanel({
  databaseId,
  selectedPageId,
  onSelectPage,
}: {
  databaseId: string | null;
  selectedPageId: number | null;
  onSelectPage: (pageId: number) => void;
}) {
  const query = usePages(databaseId);

  const subtitle = query.data
    ? `${formatCount(query.data.page_count)} pages · ${formatBytes(query.data.total_bytes)} · ${formatBytes(query.data.page_size)}/page`
    : undefined;

  return (
    <Panel title="Disk map" subtitle={subtitle} className="h-full">
      {!databaseId ? (
        <EmptyState title="No database open" hint="Create or select one above." />
      ) : query.isPending ? (
        <Spinner label="Reading pages" />
      ) : query.isError ? (
        <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <ul className="divide-y divide-[var(--border-subtle)]">
          {query.data.pages.map((page) => (
            <PageRow
              key={page.page_id}
              page={page}
              pageSize={query.data.page_size}
              selected={page.page_id === selectedPageId}
              onSelect={() => onSelectPage(page.page_id)}
            />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function PageRow({
  page,
  pageSize,
  selected,
  onSelect,
}: {
  page: PageSummaryModel;
  pageSize: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const usedPercent = 100 - percentOf(page.free_space, pageSize);

  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "true" : undefined}
        // Without this the row reads as an unnamed button: everything in it is
        // visual (a badge, a fill bar, a count).
        aria-label={`Inspect page ${page.page_id}, ${page.page_type}, owned by ${page.owner}`}
        className={cn(
          "w-full px-3 py-2 text-left transition-colors",
          selected
            ? "bg-[var(--accent)]/12"
            : "hover:bg-[var(--surface-sunken)]",
        )}
      >
        <div className="flex items-center gap-2">
          <span className="w-8 shrink-0 text-right font-mono text-xs font-semibold">
            {page.page_id}
          </span>
          <Badge tone={toneForPageType(page.page_type)}>{page.page_type}</Badge>
          <span className="text-muted min-w-0 flex-1 truncate text-[11px]">
            {page.owner}
          </span>
          {!page.checksum_valid ? <Badge tone="danger">CRC</Badge> : null}
        </div>

        <div className="mt-1.5 flex items-center gap-2">
          {/* Fill bar: how much of the page is committed to header, slots and
              records. Makes a nearly-full heap page obvious at a glance. */}
          <div
            className="surface-sunken h-1.5 flex-1 overflow-hidden rounded-full"
            title={`${formatBytes(pageSize - page.free_space)} of ${formatBytes(pageSize)} used`}
          >
            <div
              className={cn(
                "h-full rounded-full",
                usedPercent > 90 ? "bg-amber-500" : "bg-emerald-500",
              )}
              style={{ width: `${usedPercent}%` }}
            />
          </div>
          <span className="text-muted shrink-0 font-mono text-[10px]">
            {page.live_record_count > 0 || page.slot_count > 0
              ? `${page.live_record_count}/${page.slot_count} rows`
              : `@${page.file_offset}`}
          </span>
        </div>
      </button>
    </li>
  );
}
