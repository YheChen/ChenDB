/**
 * The transactions workspace.
 *
 *   ┌───────────────────────────────────────────────────────────────┐
 *   │ [ BEGIN ]  [ COMMIT ]  [ ROLLBACK ]      state · undo size    │
 *   ├───────────────────────────────────────────────────────────────┤
 *   │ try it:  [ insert, then roll back ]  [ break it half-way ]    │
 *   ├──────────────────────────────┬────────────────────────────────┤
 *   │ Undo log (page snapshots)    │ Timeline (every transaction)   │
 *   └──────────────────────────────┴────────────────────────────────┘
 *
 * The demonstrations exist because the interesting claim (*this is atomic*)
 * is only convincing if you watch it fail and leave nothing behind. "Break it
 * half-way" runs a multi-row INSERT whose last row duplicates a key: before
 * Milestone 8 the earlier rows would have stayed. Now the row count is
 * unchanged, and the reader can check that themselves in the Storage tab.
 *
 * Every button here calls the transaction endpoints or the ordinary query
 * endpoint. Nothing is simulated, and a rollback invalidates the rest of the
 * explorer's caches, because a rollback really does change the rows the other
 * panels are showing.
 */

import { useState } from "react";
import { SplitPane } from "@/components/SplitPane";
import { Button, ErrorNotice, Panel, Spinner } from "@/components/primitives";
import {
  useCatalog,
  useRunQuery,
  useTable,
  useTransactionAction,
  useTransactions,
} from "@/hooks/useEngine";
import { TRANSACTION_DEMOS } from "@/lib/demoSql";
import type { TransactionModel } from "@/types/api";
import { formatBytes, formatCount } from "@/lib/format";
import { TransactionTimeline } from "./TransactionTimeline";
import { UndoLogPanel } from "./UndoLogPanel";

export function TransactionWorkspace({
  databaseId,
  onSelectPage,
}: {
  databaseId: string;
  onSelectPage?: (pageId: number) => void;
}) {
  const [note, setNote] = useState<string | null>(null);
  const transactions = useTransactions(databaseId);
  const catalog = useCatalog(databaseId);
  const run = useRunQuery(databaseId);
  const act = useTransactionAction(databaseId);

  const firstTable = catalog.data?.tables[0]?.name ?? null;
  const table = useTable(databaseId, firstTable).data ?? null;
  const state = transactions.data;
  const active = state?.active ?? null;
  const failed = state?.is_failed ?? false;
  const busy = act.isPending || run.isPending;

  return (
    // The two panels above the split size themselves to their content, and that
    // content grows when a demonstration fails. Scrolling the workspace is the
    // honest response; letting the split collapse would hide the undo log at
    // exactly the moment it has something to show.
    <div className="flex min-h-0 w-full flex-col gap-2 overflow-y-auto">
      <Panel
        // shrink-0: this panel's height changes as the transaction does (a
        // failure adds a warning line) and without it the flex layout steals
        // the difference from the panel rather than from the split below.
        className="shrink-0"
        title="Transaction"
        subtitle={
          active
            ? `#${active.transaction_id} ${active.implicit ? "implicit" : "explicit"}`
            : "none open"
        }
        actions={
          <div className="flex items-center gap-1.5">
            <Button
              variant="primary"
              disabled={busy || state?.in_explicit_transaction}
              title={
                state?.in_explicit_transaction
                  ? "A transaction is already open. ChenDB has no savepoints, so they do not nest"
                  : "Open a transaction"
              }
              onClick={() => {
                setNote(null);
                act.mutate("begin");
              }}
            >
              BEGIN
            </Button>
            <Button
              disabled={busy || !state?.in_explicit_transaction}
              title={
                failed
                  ? "A statement in this transaction failed, so COMMIT will roll it back"
                  : "Keep the work"
              }
              onClick={() => {
                setNote(null);
                act.mutate("commit");
              }}
            >
              COMMIT
            </Button>
            <Button
              variant="danger"
              disabled={busy || !state?.in_explicit_transaction}
              onClick={() => {
                setNote(null);
                act.mutate("rollback");
              }}
            >
              ROLLBACK
            </Button>
          </div>
        }
      >
        {transactions.isPending ? (
          <Spinner label="Reading the transaction state" />
        ) : transactions.isError ? (
          <ErrorNotice
            error={transactions.error}
            onRetry={() => void transactions.refetch()}
          />
        ) : (
          <div className="space-y-2 p-3">
            <Summary
              active={active}
              failed={failed}
              undoBytes={state?.undo_bytes ?? 0}
            />
            {act.isError ? <ErrorNotice error={act.error} /> : null}
            {act.data ? (
              <p className="text-muted font-mono text-[11px]">
                {act.data.message}
              </p>
            ) : null}
          </div>
        )}
      </Panel>

      <Panel
        className="shrink-0"
        title="Try it"
        subtitle={table?.name ?? "no table to write to"}
      >
        <div className="space-y-2 p-3">
          {!table ? (
            <p className="text-muted text-[11px]">
              Create a table first. Atomicity is only visible when there is
              something to leave behind.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap gap-1.5">
                {TRANSACTION_DEMOS.map((demo) => {
                  const blocked = demo.blockedBy?.(table) ?? null;
                  return (
                    <Button
                      key={demo.id}
                      title={blocked ?? demo.hint}
                      disabled={busy || blocked !== null}
                      onClick={() => {
                        setNote(demo.hint);
                        run.mutate({ sql: demo.sql(table), maxRows: 1 });
                      }}
                    >
                      {demo.label}
                    </Button>
                  );
                })}
              </div>
              {note ? <p className="text-muted text-[11px]">{note}</p> : null}
              {run.isError ? (
                // Deliberately not an ErrorNotice. The statement failing is the
                // demonstration *working*, and a red "something went wrong"
                // banner would read as the explorer breaking.
                <p className="rounded border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-2 py-1.5 font-mono text-[11px]">
                  <span className="text-[var(--accent)]">rejected:</span>{" "}
                  {String((run.error as Error)?.message ?? run.error)}
                  <span className="text-muted block pt-0.5">
                    Which is the point. Check the row count in Storage: nothing
                    from that statement is there.
                  </span>
                </p>
              ) : null}
            </>
          )}
        </div>
      </Panel>

      {/* A floor, not just flex-1: the panels above grow when a demonstration
          fails, and without a minimum they would squeeze the undo log (the
          thing the failure was meant to show) down to nothing. */}
      <div className="min-h-[220px] flex-1">
        <SplitPane
          direction="horizontal"
          initialPercent={50}
          minPercent={25}
          maxPercent={75}
          label="Resize the undo log against the timeline"
          className="h-full"
          first={
            <div className="min-h-0 w-full pr-1">
              <Panel
                title="Undo log"
                subtitle={
                  active
                    ? `${formatCount(active.pages_held)} page(s) · ${formatBytes(active.undo_bytes)}`
                    : "empty"
                }
                className="h-full"
              >
                <UndoLogPanel
                  records={active?.records ?? []}
                  pageSize={pageSizeOf(active)}
                  onSelectPage={onSelectPage}
                />
              </Panel>
            </div>
          }
          second={
            <div className="min-h-0 w-full pl-1">
              <Panel
                title="Timeline"
                subtitle={`${formatCount(state?.history.length ?? 0)} finished`}
                className="h-full"
              >
                <TransactionTimeline
                  transactions={[
                    ...(state?.history ?? []),
                    ...(active ? [active] : []),
                  ]}
                  historyLimit={state?.history_limit ?? 50}
                />
              </Panel>
            </div>
          }
        />
      </div>
    </div>
  );
}

function Summary({
  active,
  failed,
  undoBytes,
}: {
  active: TransactionModel | null;
  failed: boolean;
  undoBytes: number;
}) {
  if (!active) {
    return (
      <p className="text-muted text-[11px]">
        Nothing open. Statements still run transactionally: the engine opens an
        implicit transaction around each one and commits it, which is why a
        multi-row INSERT that fails leaves nothing behind.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {failed ? (
        <p className="font-mono text-[11px] text-red-600 dark:text-red-400">
          A statement in this transaction failed. Nothing else will run until it
          ends. COMMIT will roll it back.
        </p>
      ) : null}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-[11px] sm:grid-cols-4">
        <Stat label="statements" value={formatCount(active.statements)} />
        <Stat
          label="page writes"
          value={formatCount(active.pages_written)}
          hint="Every write seen, including repeats of the same page"
        />
        <Stat
          label="pages held"
          value={formatCount(active.pages_held)}
          hint="Distinct pages with a before-image. First write to a page captures it; the rest are free."
        />
        <Stat
          label="undo size"
          value={formatBytes(undoBytes)}
          hint="What it costs to be able to change your mind"
        />
      </dl>
      {active.pages_written > active.pages_held ? (
        <p className="text-muted text-[11px]">
          {formatCount(active.pages_written)} writes cost{" "}
          {formatCount(active.pages_held)} before-image
          {active.pages_held === 1 ? "" : "s"}. A page is captured once,
          however many times it changes.
        </p>
      ) : null}
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div title={hint}>
      <dt className="text-muted text-[10px] uppercase tracking-wide">
        {label}
      </dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

/** The undo log holds whole pages, so any record's size is the page size. */
function pageSizeOf(active: TransactionModel | null): number {
  return active?.records?.[0]?.before_image_size ?? 0;
}
