/**
 * The Milestone 1 workspace.
 *
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ database · trace level · engine state                    │
 *   ├──────────────┬───────────────────────┬───────────────────┤
 *   │ Schema       │ Disk map              │ Page inspector    │
 *   │ Rows         │                       │                   │
 *   ├──────────────┴───────────────────────┴───────────────────┤
 *   │ Event timeline                                            │
 *   └──────────────────────────────────────────────────────────┘
 *
 * Panels for later milestones — SQL editor, plan explorer, B+ tree, buffer
 * pool, transactions, WAL — are absent rather than disabled placeholders. The
 * `features` map from `/health` is what turns them on as they land.
 */

import { useEffect, useState } from "react";
import { SplitPane } from "@/components/SplitPane";
import { EventTimeline } from "@/features/events/EventTimeline";
import { TopBar } from "@/features/layout/TopBar";
import { PageInspector } from "@/features/pages/PageInspector";
import { PageListPanel } from "@/features/pages/PageListPanel";
import { RecordsPanel } from "@/features/records/RecordsPanel";
import { SchemaPanel } from "@/features/table/SchemaPanel";
import { useDatabase, useDatabases, useTable } from "@/hooks/useEngine";
import { useEventStream } from "@/hooks/useEventStream";
import type { TraceLevelName } from "@/lib/api";

const STORAGE_KEY = "chendb.database";

export function ExplorerPage() {
  const [databaseId, setDatabaseId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const [selectedPageId, setSelectedPageId] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);

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

  useEffect(() => {
    try {
      if (databaseId) localStorage.setItem(STORAGE_KEY, databaseId);
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Storage may be unavailable; the selection still works this session.
    }
  }, [databaseId]);

  // Default to the meta page so the inspector is never empty on arrival: it is
  // the most instructive page in the file.
  useEffect(() => {
    setSelectedPageId(databaseId ? 0 : null);
  }, [databaseId]);

  const traceLevel = (database.data?.trace_level ?? "STORAGE") as TraceLevelName;

  return (
    <div className="flex h-full flex-col gap-2 p-2">
      <TopBar
        databaseId={databaseId}
        onSelectDatabase={setDatabaseId}
        traceLevel={traceLevel}
      />

      <SplitPane
        direction="vertical"
        initialPercent={66}
        minPercent={30}
        maxPercent={85}
        label="Resize the workspace against the event timeline"
        className="min-h-0 flex-1"
        first={
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
                    hasTable={table.isSuccess}
                    onSelectPage={setSelectedPageId}
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
                      onSelectPage={setSelectedPageId}
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
