/**
 * The write-ahead log workspace.
 *
 *   ┌──────────────────────────────────────────────────────────────────┐
 *   │ log size · records · fsync · commit ceiling   [Checkpoint][Crash]│
 *   ├──────────────────────────────────────────────────────────────────┤
 *   │ RECOVERY   analysis → redo → undo, from the last open            │
 *   ├──────────────────────────────────────────────────────────────────┤
 *   │ THE LOG    lsn · txn · type · page · size · undo · prev          │
 *   └──────────────────────────────────────────────────────────────────┘
 *
 * The crash button is the reason this workspace exists. Every other panel in
 * the explorer shows something the engine is doing; this one lets you *break*
 * it and watch the engine put itself back, which is the only way a durability
 * claim becomes believable rather than asserted.
 *
 * It is guarded behind a confirm, because it genuinely destroys uncommitted
 * work — that being exactly the point. What it cannot destroy is anything the
 * engine promised to keep, and the before/after row counts it reports are how
 * you check that rather than take it on trust.
 */

import { useState } from "react";
import { Button, ErrorNotice, Panel, Spinner } from "@/components/primitives";
import {
  useCheckpoint,
  useCrash,
  useFeature,
  useRecovery,
  useRunQuery,
  useTable,
  useCatalog,
  useTransactions,
  useWal,
} from "@/hooks/useEngine";
import { WAL_DEMOS } from "@/lib/demoSql";
import { formatBytes, formatCount } from "@/lib/format";
import { RecoveryPanel } from "./RecoveryPanel";
import { WalCounters, WalTable } from "./WalTable";

export function WalWorkspace({
  databaseId,
  onSelectPage,
}: {
  databaseId: string;
  onSelectPage?: (pageId: number) => void;
}) {
  const [armed, setArmed] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const wal = useWal(databaseId);
  const recovery = useRecovery(databaseId);
  const transactions = useTransactions(databaseId);
  const catalog = useCatalog(databaseId);
  const firstTable = catalog.data?.tables[0]?.name ?? null;
  const table = useTable(databaseId, firstTable).data ?? null;

  const checkpoint = useCheckpoint(databaseId);
  const crash = useCrash(databaseId);
  const run = useRunQuery(databaseId);

  const inTransaction = transactions.data?.in_explicit_transaction ?? false;
  const busy = checkpoint.isPending || crash.isPending || run.isPending;
  const durable = useFeature("durable_fsync");

  return (
    <div className="flex min-h-0 w-full flex-col gap-2 overflow-y-auto">
      <Panel
        className="shrink-0"
        title="Write-ahead log"
        subtitle={
          wal.data
            ? `${formatBytes(wal.data.size_bytes)} · ${formatCount(wal.data.total_records)} record(s)`
            : undefined
        }
        actions={
          <div className="flex items-center gap-1.5">
            <Button
              disabled={busy || inTransaction}
              title={
                inTransaction
                  ? "A transaction is open — truncating the log would discard the before-images it needs to roll back"
                  : "Flush every dirty page, then discard the log"
              }
              onClick={() => {
                setNote(null);
                setArmed(false);
                checkpoint.mutate();
              }}
            >
              Checkpoint
            </Button>
            {armed ? (
              <>
                <Button
                  variant="danger"
                  disabled={busy}
                  onClick={() => {
                    setArmed(false);
                    setNote(null);
                    crash.mutate();
                  }}
                >
                  Yes, crash it
                </Button>
                <Button onClick={() => setArmed(false)}>Cancel</Button>
              </>
            ) : (
              <Button
                variant="danger"
                disabled={busy}
                title="Abandon the handle without flushing, then reopen — which runs recovery"
                onClick={() => setArmed(true)}
              >
                Simulate crash
              </Button>
            )}
          </div>
        }
      >
        {wal.isPending ? (
          <Spinner label="Reading the log" />
        ) : wal.isError ? (
          <ErrorNotice error={wal.error} onRetry={() => void wal.refetch()} />
        ) : (
          <>
            {durable ? null : (
              // The crash really does replay the log — it is the loss of the
              // buffer pool that recovery undoes, and that is genuine here.
              // What an in-memory filesystem cannot demonstrate is a power
              // cut, because there is nothing for fsync to reach. Saying so is
              // the difference between a demonstration and a claim.
              <p className="text-muted border-b border-[var(--border-subtle)] px-3 py-2 text-[11px]">
                This build keeps its pages in memory, so <code>fsync</code> has
                nowhere to write. The crash below is real — the buffer pool is
                lost and recovery replays the log to get the rows back — but it
                proves survival of a <em>process</em> death, not a power cut.
                The <code>SIGKILL</code> tests in <code>tests/recovery/</code>{" "}
                prove the other one.
              </p>
            )}
            <WalCounters wal={wal.data} />
            {armed ? (
              <p className="border-t border-[var(--border-subtle)] px-3 py-2 text-[11px]">
                This discards everything not yet committed — dirty pages are
                dropped, no checkpoint runs, and the file is reopened as it
                lies. Committed rows survive, because their commit records were
                already on the disk. That difference is the demonstration.
              </p>
            ) : null}
            {checkpoint.isError ? (
              <div className="px-3 pb-3">
                <ErrorNotice error={checkpoint.error} />
              </div>
            ) : null}
            {crash.isError ? (
              <div className="px-3 pb-3">
                <ErrorNotice error={crash.error} />
              </div>
            ) : null}
            {checkpoint.data ? (
              <p className="text-muted border-t border-[var(--border-subtle)] px-3 py-2 font-mono text-[11px]">
                {checkpoint.data.message}
              </p>
            ) : null}
            {crash.data ? <CrashResult result={crash.data} /> : null}
          </>
        )}
      </Panel>

      <Panel
        className="shrink-0"
        title="Try it"
        subtitle={table?.name ?? "no table yet"}
      >
        <div className="space-y-2 p-3">
          {!table ? (
            <p className="text-muted text-[11px]">
              Create a table and insert some rows first — there is nothing to
              lose, and therefore nothing to demonstrate, until then.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap gap-1.5">
                {WAL_DEMOS.map((demo) => (
                  <Button
                    key={demo.id}
                    disabled={busy || (demo.requiresNoTransaction && inTransaction)}
                    title={demo.hint}
                    onClick={() => {
                      setNote(demo.note);
                      run.mutate({ sql: demo.sql(table), maxRows: 1 });
                    }}
                  >
                    {demo.label}
                  </Button>
                ))}
              </div>
              {note ? <p className="text-muted text-[11px]">{note}</p> : null}
              {run.isError ? <ErrorNotice error={run.error} /> : null}
            </>
          )}
        </div>
      </Panel>

      <Panel className="shrink-0" title="Last recovery">
        {recovery.isPending ? (
          <Spinner label="Reading the recovery report" />
        ) : recovery.isError ? (
          <ErrorNotice error={recovery.error} />
        ) : (
          <RecoveryPanel report={recovery.data} />
        )}
      </Panel>

      <div className="min-h-[220px] flex-1">
        <Panel
          title="The log"
          subtitle={
            wal.data
              ? `base LSN ${formatCount(wal.data.base_lsn)} · next ${formatCount(wal.data.next_lsn)}`
              : undefined
          }
          className="h-full"
        >
          {wal.isPending ? (
            <Spinner label="Reading the log" />
          ) : wal.isError ? (
            <ErrorNotice error={wal.error} />
          ) : (
            <WalTable wal={wal.data} onSelectPage={onSelectPage} />
          )}
        </Panel>
      </div>
    </div>
  );
}

function CrashResult({
  result,
}: {
  result: {
    message: string;
    rows_before: Record<string, number>;
    rows_after: Record<string, number>;
  };
}) {
  const tables = Object.keys(result.rows_before);
  return (
    <div className="space-y-1 border-t border-[var(--border-subtle)] px-3 py-2 font-mono text-[11px]">
      <p>{result.message}</p>
      {tables.map((name) => {
        const before = result.rows_before[name] ?? 0;
        const after = result.rows_after[name] ?? 0;
        return (
          <p key={name} className="text-muted">
            {name}: {formatCount(before)} → {formatCount(after)}
            {before === after ? (
              <span className="pl-2 text-emerald-600 dark:text-emerald-400">
                everything committed survived
              </span>
            ) : (
              <span className="pl-2 text-red-600 dark:text-red-400">
                {formatCount(before - after)} uncommitted row(s) rolled back
              </span>
            )}
          </p>
        );
      })}
    </div>
  );
}
