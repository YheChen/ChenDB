/**
 * The index workspace.
 *
 *   ┌──────────────┬──────────────────────────────────────────────┐
 *   │ indexes      │  point lookup:  [ value ]  → path, matches   │
 *   │  users_age   │──────────────────────────────────────────────│
 *   │  users_pk    │                                              │
 *   │              │      the real B+ tree, node by node          │
 *   │ + create     │                                              │
 *   └──────────────┴──────────────────────────────────────────────┘
 *
 * Everything drawn here is read out of the engine's actual pages: the page ids
 * are real page ids, and clicking a node opens it in the page inspector, where
 * the same bytes appear as a hexdump. There is no separate model of the tree in
 * the browser to drift out of sync with the one on disk.
 *
 * A traced lookup highlights the path the engine took — root to leaf, the same
 * descent :meth:`BPlusTree.search` performs — which is the clearest way to show
 * why three page reads beat scanning four thousand pages.
 */

import { useEffect, useState } from "react";
import { SplitPane } from "@/components/SplitPane";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNotice,
  Field,
  Panel,
  Spinner,
} from "@/components/primitives";
import { useCreateIndex, useIndex, useIndexSearch, useIndexes } from "@/hooks/useEngine";
import { cn, formatCount } from "@/lib/format";
import type { IndexSummary } from "@/types/api";
import { BTreeView } from "./BTreeView";

const INDEX_KEY = "chendb.index";

export function IndexWorkspace({
  databaseId,
  onSelectPage,
}: {
  databaseId: string;
  onSelectPage?: (pageId: number) => void;
}) {
  const [selected, setSelected] = useState<string | null>(() => read(INDEX_KEY));
  const [searchValue, setSearchValue] = useState("");
  const indexes = useIndexes(databaseId);

  // Select the first index automatically, and drop a selection whose index has
  // gone away — otherwise the tree panel shows a stale 404.
  useEffect(() => {
    if (!indexes.data) return;
    const names = indexes.data.indexes.map((index) => index.name);
    if (selected && names.includes(selected)) return;
    setSelected(names[0] ?? null);
  }, [indexes.data, selected]);

  useEffect(() => write(INDEX_KEY, selected), [selected]);
  useEffect(() => setSearchValue(""), [selected]);

  return (
    <SplitPane
      direction="horizontal"
      initialPercent={26}
      minPercent={18}
      maxPercent={45}
      label="Resize the index list"
      className="min-h-0 w-full"
      first={
        <div className="min-h-0 w-full pr-1">
          <IndexListPanel
            databaseId={databaseId}
            selected={selected}
            onSelect={setSelected}
          />
        </div>
      }
      second={
        <div className="flex min-h-0 w-full flex-col gap-2 pl-1">
          <SearchPanel
            databaseId={databaseId}
            indexName={selected}
            value={searchValue}
            onChange={setSearchValue}
          />
          <div className="min-h-0 flex-1">
            <TreePanel
              databaseId={databaseId}
              indexName={selected}
              searchValue={searchValue}
              onSelectPage={onSelectPage}
            />
          </div>
        </div>
      }
    />
  );
}

// -- the list ---------------------------------------------------------------

function IndexListPanel({
  databaseId,
  selected,
  onSelect,
}: {
  databaseId: string;
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  const [creating, setCreating] = useState(false);
  const query = useIndexes(databaseId);

  return (
    <Panel
      title="Indexes"
      subtitle={
        query.data ? `${formatCount(query.data.indexes.length)} index(es)` : undefined
      }
      className="h-full"
      actions={
        <Button onClick={() => setCreating((open) => !open)} aria-pressed={creating}>
          {creating ? "Cancel" : "New index"}
        </Button>
      }
    >
      {creating ? (
        <CreateIndexForm databaseId={databaseId} onDone={() => setCreating(false)} />
      ) : null}

      {query.isPending ? (
        <Spinner label="Reading indexes" />
      ) : query.isError ? (
        <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.indexes.length === 0 ? (
        <EmptyState
          title="No indexes yet"
          hint="Run CREATE INDEX in the Execution workspace, or use the button above. Every query is a sequential scan until one exists."
        />
      ) : (
        <ul className="divide-y divide-[var(--border-subtle)]">
          {query.data.indexes.map((index) => (
            <IndexRow
              key={index.index_id}
              index={index}
              selected={index.name === selected}
              onSelect={() => onSelect(index.name)}
            />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function IndexRow({
  index,
  selected,
  onSelect,
}: {
  index: IndexSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "true" : undefined}
        aria-label={
          `${index.name} on ${index.table_name}.${index.column_name}, ` +
          `${index.entry_count} entries, height ${index.height}`
        }
        className={cn(
          "w-full px-3 py-2 text-left transition-colors",
          selected ? "bg-[var(--accent)]/12" : "hover:bg-[var(--surface-sunken)]",
        )}
      >
        <div className="flex items-baseline gap-2">
          <span className="min-w-0 flex-1 truncate text-xs font-semibold">
            {index.name}
          </span>
          {index.unique ? <Badge tone="accent">unique</Badge> : null}
        </div>
        <p className="text-muted mt-0.5 font-mono text-[10px]">
          {index.table_name}.{index.column_name} · {index.data_type.toLowerCase()}
        </p>
        <p className="text-muted mt-0.5 font-mono text-[10px]">
          h{index.height} · {formatCount(index.entry_count)} entries ·{" "}
          {index.page_count} page{index.page_count === 1 ? "" : "s"}
        </p>
      </button>
    </li>
  );
}

function CreateIndexForm({
  databaseId,
  onDone,
}: {
  databaseId: string;
  onDone: () => void;
}) {
  const [name, setName] = useState("");
  const [table, setTable] = useState("");
  const [column, setColumn] = useState("");
  const [unique, setUnique] = useState(false);
  const create = useCreateIndex(databaseId);

  return (
    <form
      className="surface-sunken space-y-2 border-b border-[var(--border-subtle)] p-3"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate({ name, table, column, unique }, { onSuccess: onDone });
      }}
    >
      {(
        [
          ["Index name", name, setName],
          ["Table", table, setTable],
          ["Column", column, setColumn],
        ] as const
      ).map(([label, value, set]) => (
        <label key={label} className="block">
          <span className="text-muted text-[10px] tracking-wide uppercase">{label}</span>
          <input
            required
            value={value}
            onChange={(event) => set(event.target.value)}
            className="surface mt-0.5 w-full rounded border border-[var(--border)] px-2 py-1 font-mono text-xs"
          />
        </label>
      ))}
      <label className="flex items-center gap-2 text-xs">
        <input
          type="checkbox"
          checked={unique}
          onChange={(event) => setUnique(event.target.checked)}
        />
        <span>
          unique
          <span className="text-muted">: rejects duplicates, NULLs are exempt</span>
        </span>
      </label>
      {create.isError ? <ErrorNotice error={create.error} /> : null}
      <Button type="submit" disabled={create.isPending}>
        {create.isPending ? "Building…" : "Create index"}
      </Button>
    </form>
  );
}

// -- traced lookup ----------------------------------------------------------

function SearchPanel({
  databaseId,
  indexName,
  value,
  onChange,
}: {
  databaseId: string;
  indexName: string | null;
  value: string;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const search = useIndexSearch(databaseId, indexName, value);

  return (
    <Panel title="Point lookup" subtitle={indexName ?? "select an index"}>
      <div className="space-y-2 p-3">
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            onChange(draft);
          }}
        >
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="a key to look up"
            aria-label="Key to look up"
            disabled={!indexName}
            className="surface-sunken min-w-0 flex-1 rounded border border-[var(--border)] px-2 py-1 font-mono text-xs"
          />
          <Button type="submit" disabled={!indexName}>
            Search
          </Button>
          {value ? <Button onClick={() => onChange("")}>Clear</Button> : null}
        </form>

        {!value ? (
          <p className="text-muted text-[11px]">
            Enter a key to trace the descent. The path from the root is
            highlighted in the tree below.
          </p>
        ) : search.isPending ? (
          <Spinner label="Descending" />
        ) : search.isError ? (
          <ErrorNotice error={search.error} />
        ) : (
          <div className="space-y-2">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
              <Field label="found" value={search.data.found ? "yes" : "no"} />
              <Field label="matches" value={search.data.matches.length} />
              <Field
                label="pages read"
                value={search.data.pages_visited}
                title="Nodes touched. Equal to the height for a clean descent, more when duplicates span leaves and the search steps right."
              />
              <Field label="height" value={search.data.height} />
            </dl>
            <p className="text-muted font-mono text-[10px]">
              path: {search.data.path.map((page) => `p${page}`).join(" → ")}
            </p>
            {search.data.matches.length > 0 ? (
              <p className="text-muted font-mono text-[10px]">
                rows: {search.data.matches.slice(0, 12).join(" ")}
                {search.data.matches.length > 12
                  ? ` … +${search.data.matches.length - 12}`
                  : ""}
              </p>
            ) : null}
          </div>
        )}
      </div>
    </Panel>
  );
}

// -- the tree ---------------------------------------------------------------

function TreePanel({
  databaseId,
  indexName,
  searchValue,
  onSelectPage,
}: {
  databaseId: string;
  indexName: string | null;
  searchValue: string;
  onSelectPage?: (pageId: number) => void;
}) {
  const query = useIndex(databaseId, indexName);
  const search = useIndexSearch(databaseId, indexName, searchValue);

  return (
    <Panel
      title="B+ tree"
      subtitle={
        query.data
          ? `height ${query.data.tree.height} · ${query.data.tree.nodes.length} node(s)` +
            (query.data.tree.truncated ? " · truncated" : "")
          : undefined
      }
      className="h-full"
    >
      {!indexName ? (
        <EmptyState
          title="No index selected"
          hint="Choose one on the left to see its tree."
        />
      ) : query.isPending ? (
        <Spinner label="Reading the tree" />
      ) : query.isError ? (
        <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <div className="space-y-1">
          {query.data.tree.truncated ? (
            <p className="border-b border-[var(--border-subtle)] bg-amber-500/10 px-3 py-1.5 text-[11px]">
              Only the first {query.data.tree.nodes.length} nodes are shown. The tree
              is larger than the display budget.
            </p>
          ) : null}
          <BTreeView
            tree={query.data.tree}
            highlightedPath={search.data?.path ?? []}
            onSelectPage={onSelectPage}
          />
          <TreeLegend
            stats={query.data.stats}
            entryCount={query.data.index.entry_count}
          />
        </div>
      )}
    </Panel>
  );
}

function TreeLegend({
  stats,
  entryCount,
}: {
  stats: { splits: number; root_splits: number; searches: number; inserts: number };
  entryCount: number;
}) {
  return (
    <div className="border-t border-[var(--border-subtle)] px-3 py-2">
      <div className="text-muted mb-1.5 flex flex-wrap gap-3 text-[10px]">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-4 rounded-sm bg-sky-500/40" /> internal
          (separators only)
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-4 rounded-sm bg-emerald-500/40" /> leaf
          (keys + record ids)
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-px w-4 border-t border-dashed border-[var(--accent)]" />{" "}
          sibling link
        </span>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-5">
        <Field label="entries" value={formatCount(entryCount)} />
        <Field label="inserts" value={formatCount(stats.inserts)} />
        <Field
          label="splits"
          value={stats.splits}
          title="A node overflowed and was cut in two, pushing a separator into its parent."
        />
        <Field
          label="root splits"
          value={stats.root_splits}
          title="The only operation that changes the tree's height: every leaf gets one level deeper at once."
        />
        <Field label="searches" value={formatCount(stats.searches)} />
      </dl>
      <p className="text-muted mt-1.5 text-[10px]">
        Sizes are real bytes on real pages. Click a node to open it in the page
        inspector.
      </p>
    </div>
  );
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // Storage may be unavailable; the selection still works this session.
  }
}
