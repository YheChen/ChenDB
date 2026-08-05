/**
 * The workspace shell.
 *
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ database · trace level · engine state                    │
 *   ├──────────────────────────────────────────────────────────┤
 *   │ [ Storage ] [ SQL ]                          ← workspaces │
 *   ├──────────────────────────────────────────────────────────┤
 *   │                                                          │
 *   │  Storage:   Schema · Rows │ Disk map │ Page inspector     │
 *   │  SQL:       Editor        │ Tokens / AST                  │
 *   │  Execution: Editor        │ Plan · Results · Step controls│
 *   │  Indexes:   Index list    │ Point lookup · B+ tree        │
 *   │  Buffer:    Counters      │ Workloads │ Frame grid        │
 *   │  Txns:      BEGIN/COMMIT  │ Undo log  │ Timeline          │
 *   │  WAL:       Checkpoint/Crash │ Recovery │ The log           │
 *   │  MVCC:      two consoles     │ Locks    │ wait-for graph    │
 *   │                                                          │
 *   ├──────────────────────────────────────────────────────────┤
 *   │ Event timeline (shared by every workspace)               │
 *   └──────────────────────────────────────────────────────────┘
 *
 * A workspace tab appears only when `/health` says its feature exists. The SQL
 * tab was absent in Milestone 1 and appears in Milestone 2, Execution in
 * Milestone 3, Indexes in Milestone 5; later milestones add Plan, Buffer pool,
 * Transactions and WAL the same way. Nothing is ever shown greyed out for a
 * feature the engine does not have.
 */

import { useEffect, useRef, useState } from "react";
import { SplitPane } from "@/components/SplitPane";
import { cn } from "@/lib/format";
import {
  CatalogPanel,
  TableDetailPanel,
} from "@/features/catalog/CatalogPanel";
import { EventTimeline } from "@/features/events/EventTimeline";
import { TopBar } from "@/features/layout/TopBar";
import { PageInspector } from "@/features/pages/PageInspector";
import { PageListPanel } from "@/features/pages/PageListPanel";
import { RecordsPanel } from "@/features/records/RecordsPanel";
import { ExecutionWorkspace } from "@/features/execution/ExecutionWorkspace";
import { BufferWorkspace } from "@/features/buffer/BufferWorkspace";
import { TransactionWorkspace } from "@/features/transactions/TransactionWorkspace";
import { WalWorkspace } from "@/features/wal/WalWorkspace";
import { ConcurrencyWorkspace } from "@/features/concurrency/ConcurrencyWorkspace";
import { IndexWorkspace } from "@/features/indexes/IndexWorkspace";
import { SqlWorkspace } from "@/features/sql/SqlWorkspace";
import {
  useCatalog,
  useDatabase,
  useDatabases,
  useSeedSampleDatabase,
  useHealth,
} from "@/hooks/useEngine";
import { useEventStream } from "@/hooks/useEventStream";
import { useTheme } from "@/hooks/useTheme";
import type { TraceLevelName } from "@/lib/api";

const DATABASE_KEY = "chendb.database";
const WORKSPACE_KEY = "chendb.workspace";
const TABLE_KEY = "chendb.table";

type WorkspaceId =
  | "storage"
  | "sql"
  | "execution"
  | "indexes"
  | "buffer"
  | "transactions"
  | "wal"
  | "mvcc";

const WORKSPACES: { id: WorkspaceId; label: string; feature: string }[] = [
  { id: "storage", label: "Storage", feature: "storage" },
  { id: "sql", label: "SQL", feature: "sql" },
  { id: "execution", label: "Execution", feature: "execution" },
  { id: "indexes", label: "Indexes", feature: "indexes" },
  { id: "buffer", label: "Buffer pool", feature: "buffer_pool" },
  { id: "transactions", label: "Transactions", feature: "transactions" },
  { id: "wal", label: "WAL", feature: "wal" },
  { id: "mvcc", label: "MVCC", feature: "mvcc" },
];

export function ExplorerPage() {
  const [databaseId, setDatabaseId] = useState<string | null>(() =>
    read(DATABASE_KEY),
  );
  const [workspace, setWorkspace] = useState<WorkspaceId>(
    () => (read(WORKSPACE_KEY) as WorkspaceId | null) ?? "storage",
  );
  const [selectedPageId, setSelectedPageId] = useState<number | null>(null);
  const [selectedTable, setSelectedTable] = useState<string | null>(() =>
    read(TABLE_KEY),
  );
  const [paused, setPaused] = useState(false);
  const [theme] = useTheme();

  const health = useHealth();
  const databases = useDatabases();
  const database = useDatabase(databaseId);
  const catalog = useCatalog(databaseId);
  const stream = useEventStream(databaseId, { paused });

  // Fall back to the first available database when none is chosen, or when the
  // remembered one has been deleted.
  useEffect(() => {
    if (!databases.data) return;
    const available = databases.data.databases.map(
      (entry) => entry.database_id,
    );
    if (databaseId && available.includes(databaseId)) return;
    setDatabaseId(available[0] ?? null);
  }, [databases.data, databaseId]);

  // Nothing to explore, so make something. The explorer used to open on eight
  // panels all reading "No database open", which is accurate and useless: every
  // panel needs a database and a first-time visitor has no reason to know that
  // `+ New` is the way in.
  //
  // Once, and only when the workspace is genuinely empty. A visitor who deletes
  // the sample meant to delete it, and a ref rather than state because two
  // renders before the mutation settles would otherwise create it twice.
  const seedAttempted = useRef(false);
  const seedSample = useSeedSampleDatabase();
  useEffect(() => {
    if (!databases.data || seedAttempted.current) return;
    seedAttempted.current = true;
    if (databases.data.databases.length > 0) return;
    seedSample.mutate(undefined, {
      onSuccess: (id) => setDatabaseId(id),
    });
  }, [databases.data, seedSample]);

  useEffect(() => write(DATABASE_KEY, databaseId), [databaseId]);
  useEffect(() => write(WORKSPACE_KEY, workspace), [workspace]);
  useEffect(() => write(TABLE_KEY, selectedTable), [selectedTable]);

  // Select the first table automatically, and drop a selection whose table has
  // gone away, otherwise the detail panel shows a stale 404.
  useEffect(() => {
    if (!catalog.data) return;
    const names = catalog.data.tables.map((table) => table.name);
    const systemNames = catalog.data.system_tables.map((table) => table.name);
    if (selectedTable && [...names, ...systemNames].includes(selectedTable))
      return;
    setSelectedTable(names[0] ?? null);
  }, [catalog.data, selectedTable]);

  // Default to the meta page: it is the most instructive page in the file, and
  // an empty inspector on arrival teaches nothing. Keyed on the database only,
  // so switching workspaces (or arriving from a click on a B+ tree node)
  // keeps whatever page is selected.
  useEffect(() => setSelectedPageId(databaseId ? 0 : null), [databaseId]);

  const features = health.data?.features ?? {};
  const available = WORKSPACES.filter(
    (entry) => features[entry.feature] !== false,
  );
  const traceLevel = (database.data?.trace_level ??
    "STORAGE") as TraceLevelName;

  return (
    <div className="flex h-full flex-col gap-2 p-2">
      <TopBar
        databaseId={databaseId}
        onSelectDatabase={setDatabaseId}
        traceLevel={traceLevel}
      />

      {available.length > 1 ? (
        <nav
          role="tablist"
          aria-label="Workspace"
          className="surface flex shrink-0 gap-1 rounded-lg border px-2 py-1.5"
        >
          {available.map((entry) => (
            <button
              key={entry.id}
              role="tab"
              type="button"
              aria-selected={workspace === entry.id}
              onClick={() => setWorkspace(entry.id)}
              className={cn(
                "rounded px-3 py-1 text-xs font-medium transition-colors",
                workspace === entry.id
                  ? "bg-[var(--accent)] text-white"
                  : "hover:bg-[var(--surface-sunken)]",
              )}
            >
              {entry.label}
            </button>
          ))}
        </nav>
      ) : null}

      <SplitPane
        direction="vertical"
        initialPercent={66}
        minPercent={30}
        maxPercent={85}
        label="Resize the workspace against the event timeline"
        className="min-h-0 flex-1"
        first={
          workspace === "mvcc" && databaseId ? (
            <ConcurrencyWorkspace databaseId={databaseId} />
          ) : workspace === "wal" && databaseId ? (
            <WalWorkspace
              databaseId={databaseId}
              onSelectPage={(pageId) => {
                setSelectedPageId(pageId);
                setWorkspace("storage");
              }}
            />
          ) : workspace === "transactions" && databaseId ? (
            <TransactionWorkspace
              databaseId={databaseId}
              onSelectPage={(pageId) => {
                setSelectedPageId(pageId);
                setWorkspace("storage");
              }}
            />
          ) : workspace === "buffer" && databaseId ? (
            <BufferWorkspace
              databaseId={databaseId}
              onSelectPage={(pageId) => {
                setSelectedPageId(pageId);
                setWorkspace("storage");
              }}
            />
          ) : workspace === "indexes" && databaseId ? (
            <IndexWorkspace
              databaseId={databaseId}
              onSelectPage={(pageId) => {
                // Jump to the Storage tab so the click actually shows the page:
                // the inspector lives there, and a click that silently changed
                // hidden state would read as broken.
                setSelectedPageId(pageId);
                setWorkspace("storage");
              }}
            />
          ) : workspace === "execution" && databaseId ? (
            <ExecutionWorkspace
              databaseId={databaseId}
              theme={theme}
              onSelectPage={setSelectedPageId}
            />
          ) : workspace === "sql" && databaseId ? (
            <SqlWorkspace databaseId={databaseId} theme={theme} />
          ) : (
            <StorageWorkspace
              databaseId={databaseId}
              selectedTable={selectedTable}
              onSelectTable={setSelectedTable}
              selectedPageId={selectedPageId}
              onSelectPage={setSelectedPageId}
            />
          )
        }
        second={
          <div className="min-h-0 w-full pt-1">
            <EventTimeline
              events={stream.events}
              connection={stream.connection}
              droppedByServer={stream.droppedByServer}
              droppedByClient={stream.droppedByClient}
              totalReceived={stream.totalReceived}
              paused={paused}
              onTogglePause={() => setPaused((current) => !current)}
              onClear={stream.clear}
            />
          </div>
        }
      />
    </div>
  );
}

function StorageWorkspace({
  databaseId,
  selectedTable,
  onSelectTable,
  selectedPageId,
  onSelectPage,
}: {
  databaseId: string | null;
  selectedTable: string | null;
  onSelectTable: (table: string | null) => void;
  selectedPageId: number | null;
  onSelectPage: (pageId: number) => void;
}) {
  return (
    <SplitPane
      direction="horizontal"
      initialPercent={30}
      minPercent={20}
      maxPercent={48}
      label="Resize the catalog column"
      className="min-h-0 w-full"
      first={
        <div className="flex min-h-0 w-full flex-col gap-2 pr-1">
          <div className="min-h-0 flex-1">
            <CatalogPanel
              databaseId={databaseId}
              selectedTable={selectedTable}
              onSelectTable={onSelectTable}
            />
          </div>
          <div className="min-h-0 flex-1">
            <TableDetailPanel
              databaseId={databaseId}
              table={selectedTable}
              onSelectPage={onSelectPage}
            />
          </div>
          <div className="min-h-0 flex-1">
            <RecordsPanel
              databaseId={databaseId}
              table={selectedTable}
              onSelectPage={onSelectPage}
            />
          </div>
        </div>
      }
      second={
        <SplitPane
          direction="horizontal"
          initialPercent={38}
          minPercent={22}
          maxPercent={60}
          label="Resize the disk map against the page inspector"
          className="min-h-0 w-full pl-1"
          first={
            <div className="min-h-0 w-full pr-1">
              <PageListPanel
                databaseId={databaseId}
                selectedPageId={selectedPageId}
                onSelectPage={onSelectPage}
              />
            </div>
          }
          second={
            <div className="min-h-0 w-full pl-1">
              <PageInspector databaseId={databaseId} pageId={selectedPageId} />
            </div>
          }
        />
      }
    />
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
    // Storage may be unavailable; selections still work this session.
  }
}
