/**
 * The buffer pool workspace.
 *
 *   ┌──────────────────────────────────────────────────────────────┐
 *   │ hit rate · resident · evictions · reads and writes avoided   │
 *   ├──────────────────────────────────────────────────────────────┤
 *   │ workloads:  [ scan ] [ scan twice ] [ point lookups ] [ … ]  │
 *   ├──────────────────────────────────────────────────────────────┤
 *   │                    the frame grid                            │
 *   └──────────────────────────────────────────────────────────────┘
 *
 * The workloads are the point. A cache's behaviour is only legible when you can
 * *cause* it: run a scan twice and watch the second one hit; run a scan of a
 * table bigger than the pool and watch it hit nothing at all, because every page
 * it wants was evicted by the pages behind it. That last one — sequential
 * flooding — is the reason real systems do not use plain LRU, and it is far
 * more convincing to watch than to read.
 *
 * Each button runs real SQL through the ordinary query endpoint. Nothing here
 * is simulated.
 */

import { useState } from "react";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Panel,
  Spinner,
} from "@/components/primitives";
import { useBufferPool, useCatalog, useRunQuery } from "@/hooks/useEngine";
import { WORKLOADS } from "@/lib/demoSql";
import { FrameGrid, PoolCounters } from "./FrameGrid";

export function BufferWorkspace({
  databaseId,
  onSelectPage,
}: {
  databaseId: string;
  onSelectPage?: (pageId: number) => void;
}) {
  const [live, setLive] = useState(true);
  const [note, setNote] = useState<string | null>(null);
  const pool = useBufferPool(databaseId, { refetchInterval: live ? 800 : false });
  const catalog = useCatalog(databaseId);
  const run = useRunQuery(databaseId);

  const table = catalog.data?.tables[0]?.name ?? null;

  return (
    <div className="flex min-h-0 w-full flex-col gap-2">
      <Panel
        title="Buffer pool"
        subtitle={
          pool.data
            ? `${pool.data.resident}/${pool.data.capacity} frames · ` +
              `${(pool.data.stats.hit_rate * 100).toFixed(0)}% hit rate`
            : undefined
        }
        actions={
          <Button onClick={() => setLive((on) => !on)} aria-pressed={live}>
            {live ? "Pause polling" : "Resume polling"}
          </Button>
        }
      >
        {pool.isPending ? (
          <Spinner label="Reading the pool" />
        ) : pool.isError ? (
          <ErrorNotice error={pool.error} onRetry={() => void pool.refetch()} />
        ) : (
          <PoolCounters pool={pool.data} />
        )}
      </Panel>

      <Panel title="Workloads" subtitle={table ?? "no table to read"}>
        <div className="space-y-2 p-3">
          {!table ? (
            <p className="text-muted text-[11px]">
              Create a table and insert some rows first — the pool has nothing to
              cache until the engine reads something.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap gap-1.5">
                {WORKLOADS.map((workload) => (
                  <Button
                    key={workload.id}
                    title={workload.hint}
                    disabled={run.isPending}
                    onClick={() => {
                      setNote(workload.hint);
                      run.mutate({ sql: workload.sql(table), maxRows: 1 });
                    }}
                  >
                    {workload.label}
                  </Button>
                ))}
              </div>
              {note ? <p className="text-muted text-[11px]">{note}</p> : null}
              {run.isError ? <ErrorNotice error={run.error} /> : null}
            </>
          )}
        </div>
      </Panel>

      <div className="min-h-0 flex-1">
        <Panel
          title="Frames"
          subtitle={
            pool.data
              ? `${pool.data.page_size} B per page · coldest is evicted next`
              : undefined
          }
          className="h-full"
        >
          {pool.isPending ? (
            <Spinner label="Reading the pool" />
          ) : pool.isError ? (
            <EmptyState title="Unavailable" hint="The pool could not be read." />
          ) : (
            <FrameGrid pool={pool.data} onSelectPage={onSelectPage} />
          )}
        </Panel>
      </div>
    </div>
  );
}
