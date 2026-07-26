/**
 * The workspace shell.
 *
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ database · trace level · engine state                    │
 *   ├──────────────────────────────────────────────────────────┤
 *   │ [ Storage ] [ SQL ]                          ← workspaces │
 *   ├──────────────────────────────────────────────────────────┤
 *   │                                                          │
 *   │  Storage: Schema · Rows │ Disk map │ Page inspector       │
 *   │  SQL:     Editor        │ Tokens / AST                    │
 *   │                                                          │
 *   ├──────────────────────────────────────────────────────────┤
 *   │ Event timeline (shared by every workspace)               │
 *   └──────────────────────────────────────────────────────────┘
 *
 * A workspace tab appears only when `/health` says its feature exists. The SQL
 * tab was absent in Milestone 1 and appears in Milestone 2; later milestones add
 * Plan, Index, Buffer pool, Transactions and WAL the same way. Nothing is ever
 * shown greyed out for a feature the engine does not have.
 */

import { useEffect, useState } from "react";
import { SplitPane } from "@/components/SplitPane";
import { cn } from "@/lib/format";
import { EventTimeline } from "@/features/events/EventTimeline";
import { TopBar } from "@/features/layout/TopBar";
import { PageInspector } from "@/features/pages/PageInspector";
import { PageListPanel } from "@/features/pages/PageListPanel";
import { RecordsPanel } from "@/features/records/RecordsPanel";
import { SqlWorkspace } from "@/features/sql/SqlWorkspace";
import { SchemaPanel } from "@/features/table/SchemaPanel";
import { useDatabase, useDatabases, useHealth, useTable } from "@/hooks/useEngine";
import { useEventStream } from "@/hooks/useEventStream";
import { useTheme } from "@/hooks/useTheme";
import type { TraceLevelName } from "@/lib/api";

const DATABASE_KEY = "chendb.database";
const WORKSPACE_KEY = "chendb.workspace";

type WorkspaceId = "storage" | "sql";

const WORKSPACES: { id: WorkspaceId; label: string; feature: string }[] = [
  { id: "storage", label: "Storage", feature: "storage" },
  { id: "sql", label: "SQL", feature: "sql" },
];

export function ExplorerPage() {
  const [databaseId, setDatabaseId] = useState<string | null>(() => read(DATABASE_KEY));
  const [workspace, setWorkspace] = useState<WorkspaceId>(
    () => (read(WORKSPACE_KEY) as WorkspaceId | null) ?? "storage",
  );
  const [selectedPageId, setSelectedPageId] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [theme] = useTheme();

  const health = useHealth();
  const databases = useDatabases();
  const database = useDatabase(databaseId);
  const table = useTable(databaseId);
  const stream = useEventStream(databaseId, { paused });

  // Fall back to the first available database when none is chosen, or when the
  // remembered one has been deleted.
  useEffect(() => {
    if (!databases.data) return;
    const available = databases.data.databases.map((entry) => entry.database_id);
    if (databaseId && available.includes(databaseId)) return;
    setDatabaseId(available[0] ?? null);
  }, [databases.data, databaseId]);

  useEffect(() => write(DATABASE_KEY, databaseId), [databaseId]);
  useEffect(() => write(WORKSPACE_KEY, workspace), [workspace]);

  // Default to the meta page: it is the most instructive page in the file, and
  // an empty inspector on arrival teaches nothing.
  useEffect(() => setSelectedPageId(databaseId ? 0 : null), [databaseId]);

  const features = health.data?.features ?? {};
  const available = WORKSPACES.filter((entry) => features[entry.feature] !== false);
  const traceLevel = (database.data?.trace_level ?? "STORAGE") as TraceLevelName;

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
          workspace === "sql" && databaseId ? (
            <SqlWorkspace databaseId={databaseId} theme={theme} />
          ) : (
            <StorageWorkspace
              databaseId={databaseId}
              hasTable={table.isSuccess}
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
  hasTable,
  selectedPageId,
  onSelectPage,
}: {
  databaseId: string | null;
  hasTable: boolean;
  selectedPageId: number | null;
  onSelectPage: (pageId: number) => void;
}) {
  return (
    <SplitPane
      direction="horizontal"
      initialPercent={28}
      minPercent={18}
      maxPercent={45}
      label="Resize the schema column"
      className="min-h-0 w-full"
      first={
        <div className="flex min-h-0 w-full flex-col gap-2 pr-1">
          <div className="min-h-0 flex-1">
            <SchemaPanel databaseId={databaseId} onTableChange={() => undefined} />
          </div>
          <div className="min-h-0 flex-1">
            <RecordsPanel
              databaseId={databaseId}
              hasTable={hasTable}
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
