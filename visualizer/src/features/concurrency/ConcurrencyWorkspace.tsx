/**
 * The concurrency workspace: two consoles, one database.
 *
 *   ┌──────────────────────────────────────────────────────────────────┐
 *   │ frozen · next · vacuum horizon              [ Vacuum ]           │
 *   ├───────────────────────────────┬──────────────────────────────────┤
 *   │ alice                         │ bob                              │
 *   │ BEGIN / COMMIT / ROLLBACK     │ BEGIN / COMMIT / ROLLBACK        │
 *   │ snapshot xmin=7 xmax=9        │ snapshot xmin=9 xmax=9           │
 *   │ SELECT * FROM users;          │ INSERT INTO users VALUES (…)     │
 *   ├───────────────────────────────┴──────────────────────────────────┤
 *   │ LOCKS   resource · held by · waiting · the wait-for graph        │
 *   └──────────────────────────────────────────────────────────────────┘
 *
 * Everything in this project up to Milestone 9 could be shown with one console.
 * This cannot: "a reader does not wait for a writer" is a statement about two
 * things happening at once, and a single console can only ever demonstrate one
 * of them.
 *
 * The scripted walkthroughs exist because the interesting states are two or
 * three steps deep and easy to get wrong by hand — and getting them wrong looks
 * exactly like the engine misbehaving.
 */

import { useState } from "react";
import { SplitPane } from "@/components/SplitPane";
import { Button, ErrorNotice, Panel, Spinner } from "@/components/primitives";
import {
  useCatalog,
  useLocks,
  useSessions,
  useTable,
  useVacuum,
} from "@/hooks/useEngine";
import { insertRow, literalFor } from "@/lib/demoRows";
import { formatCount } from "@/lib/format";
import type { TableDetail } from "@/types/api";
import { LockCounters, LockTable } from "./LockTable";
import { SessionConsole } from "./SessionConsole";

const ALICE = "alice";
const BOB = "bob";

type Walkthrough = {
  id: string;
  label: string;
  hint: string;
  alice: string;
  bob: string;
};

/**
 * Scripts built from the table that is actually open, never from a guess.
 *
 * These used to hardcode `WHERE id = 1`, which is fine until the open table has
 * no `id` — and the walkthrough then failed in a way that looked like the
 * engine misbehaving rather than the button being wrong. See `demoRows.ts`.
 */
const WALKTHROUGHS: (table: TableDetail) => Walkthrough[] = (table) => {
  const name = table.name;
  const walkthroughs: Walkthrough[] = [
    {
      id: "invisible",
      label: "A reader does not wait",
      hint: "Run bob's BEGIN and INSERT, then alice's SELECT. Alice returns immediately and does not see the row — she read an older version rather than waiting for the newer one. Then commit bob and run alice again.",
      bob: insertRow(table, 9001),
      alice: `SELECT * FROM ${name};`,
    },
    {
      id: "repeatable",
      label: "Two levels, two answers",
      hint: "Open alice's transaction, run her SELECT, then have bob insert and commit, then run alice's SELECT again. Under read committed she sees the new row; the same sequence under repeatable read would not.",
      alice: `SELECT * FROM ${name};`,
      bob: insertRow(table, 9002),
    },
  ];

  // An UPDATE needs a column to write to. Every table has one, but the type
  // does not say so, and inventing a name here is exactly the bug this file
  // used to have.
  const target = updateTarget(table);
  if (target) {
    walkthroughs.push(
      {
        id: "versions",
        label: "One row, two versions",
        hint: `BEGIN bob, run his UPDATE, then run alice's SELECT: she reads the OLD ${target.name}, because both versions are on the page at once and bob's is not committed. Commit bob and run alice again to see the new one. Then look at rows vs versions on the Storage tab, and press Vacuum.`,
        alice: `SELECT * FROM ${name};`,
        bob: `UPDATE ${name} SET ${target.name} = ${literalFor(target, 7777)};`,
      },
      {
        id: "locks",
        label: "A writer locks, a reader does not",
        hint: `BEGIN bob and run his UPDATE without committing. The lock table below fills with one exclusive lock per version he touched — two per row, the old one and the new. Now run alice's SELECT: it returns instantly, holds 0 locks, and "readers blocked" stays 0. That is the whole claim of MVCC, in one panel.`,
        alice: `SELECT * FROM ${name};`,
        bob: `UPDATE ${name} SET ${target.name} = ${literalFor(target, 200)};`,
      },
      {
        id: "rollback",
        label: "A rollback leaves nothing behind",
        hint: "BEGIN bob, run his UPDATE, and check rows vs versions on the Storage tab: rows unchanged, versions up by one per row. Now ROLLBACK and look again — the new versions are gone, not merely dead, and Vacuum has nothing to do. ChenDB undoes by restoring pages, which is why it needs no commit log; PostgreSQL, which does not undo, needs both CLOG and a vacuum pass for the rows an aborted transaction left.",
        alice: `SELECT * FROM ${name};`,
        bob: `UPDATE ${name} SET ${target.name} = ${literalFor(target, 300)};`,
      },
    );
  }
  return walkthroughs;
};

/**
 * A column worth writing a demonstration value into.
 *
 * Not the primary key if anything else is available: setting every row's `id`
 * to the same number makes the table nonsense, and a reader trying to follow
 * the version chain then cannot tell the rows apart.
 */
function updateTarget(table: TableDetail) {
  return table.columns.find((column) => !column.primary_key) ?? table.columns[0];
}

export function ConcurrencyWorkspace({ databaseId }: { databaseId: string }) {
  const [note, setNote] = useState<string | null>(null);
  const [scripts, setScripts] = useState<{ alice: string; bob: string } | null>(
    null,
  );

  const sessions = useSessions(databaseId);
  const locks = useLocks(databaseId);
  const catalog = useCatalog(databaseId);
  const vacuum = useVacuum(databaseId);

  const table = catalog.data?.tables[0]?.name ?? "users";
  // The walkthroughs write real SQL against real columns, so they need the
  // schema and not just the name.
  const detail = useTable(databaseId, catalog.data?.tables[0]?.name ?? null);
  const byName = new Map(
    (sessions.data?.sessions ?? []).map((entry) => [entry.session, entry]),
  );

  // A reader and a writer, which is the shape of every demonstration here. The
  // consoles are keyed on these, so the writer's default becomes a real INSERT
  // the moment the schema arrives rather than staying at whatever could be
  // written without one.
  const aliceSql = scripts?.alice ?? `SELECT * FROM ${table};`;
  const bobSql =
    scripts?.bob ??
    (detail.data ? insertRow(detail.data, 9000) : `SELECT * FROM ${table};`);

  return (
    <div className="flex min-h-0 w-full flex-col gap-2 overflow-y-auto">
      <Panel
        className="shrink-0"
        title="MVCC"
        subtitle={
          sessions.data
            ? `${formatCount(sessions.data.sessions.length)} session(s)`
            : undefined
        }
        actions={
          <Button
            disabled={vacuum.isPending}
            title="Reclaim row versions no open snapshot can still want"
            onClick={() => {
              setNote(null);
              vacuum.mutate();
            }}
          >
            Vacuum
          </Button>
        }
      >
        {sessions.isPending ? (
          <Spinner label="Reading the sessions" />
        ) : sessions.isError ? (
          <ErrorNotice
            error={sessions.error}
            onRetry={() => void sessions.refetch()}
          />
        ) : (
          <>
            <dl className="grid grid-cols-3 gap-x-4 gap-y-2 p-3 font-mono text-[11px]">
              <Stat
                label="frozen xid"
                value={formatCount(sessions.data.frozen_xid)}
                hint="Ids below this committed before this process started. ChenDB's entire commit log, in one number — possible only because a rollback here physically removes the work."
              />
              <Stat
                label="next xid"
                value={formatCount(sessions.data.next_xid)}
              />
              <Stat
                label="vacuum horizon"
                value={formatCount(sessions.data.oldest_snapshot_xmin)}
                hint="The oldest open snapshot. Nothing deleted at or above this can be reclaimed — a long-running transaction holds it down, which is the same mechanism behind PostgreSQL's most common 'why is my disk full'."
              />
            </dl>
            {vacuum.data ? (
              <p className="text-muted border-t border-[var(--border-subtle)] px-3 py-2 font-mono text-[11px]">
                {vacuum.data.message}
              </p>
            ) : null}
            {vacuum.isError ? (
              <div className="px-3 pb-3">
                <ErrorNotice error={vacuum.error} />
              </div>
            ) : null}
          </>
        )}
      </Panel>

      <Panel className="shrink-0" title="Walkthroughs" subtitle={table}>
        <div className="space-y-2 p-3">
          <div className="flex flex-wrap gap-1.5">
            {/* Disabled rather than hidden while the schema loads, and
                disabled rather than guessing if there is no table to use. */}
            {detail.data ? (
              WALKTHROUGHS(detail.data).map((walkthrough) => (
                <Button
                  key={walkthrough.id}
                  title={walkthrough.hint}
                  onClick={() => {
                    setNote(walkthrough.hint);
                    setScripts({
                      alice: walkthrough.alice,
                      bob: walkthrough.bob,
                    });
                  }}
                >
                  {walkthrough.label}
                </Button>
              ))
            ) : (
              <Button disabled>
                {detail.isPending && catalog.data?.tables.length
                  ? "Reading the schema…"
                  : "No table to demonstrate on"}
              </Button>
            )}
          </div>
          <p className="text-muted text-[11px]">
            {note ??
              "Each of these loads a statement into both consoles. You run them — in the order the hint gives — because the order is the thing being demonstrated."}
          </p>
        </div>
      </Panel>

      <div className="min-h-[320px] flex-1">
        <SplitPane
          direction="horizontal"
          initialPercent={50}
          minPercent={25}
          maxPercent={75}
          label="Resize the two consoles against each other"
          className="h-full"
          first={
            <div className="min-h-0 w-full pr-1">
              <SessionConsole
                key={`${ALICE}-${aliceSql}`}
                databaseId={databaseId}
                session={ALICE}
                info={byName.get(ALICE)}
                defaultSql={aliceSql}
              />
            </div>
          }
          second={
            <div className="min-h-0 w-full pl-1">
              <SessionConsole
                key={`${BOB}-${bobSql}`}
                databaseId={databaseId}
                session={BOB}
                info={byName.get(BOB)}
                defaultSql={bobSql}
              />
            </div>
          }
        />
      </div>

      <Panel
        className="min-h-[160px] shrink-0"
        title="Locks"
        subtitle="writers only — a reader takes none"
      >
        {locks.isPending ? (
          <Spinner label="Reading the lock table" />
        ) : locks.isError ? (
          <ErrorNotice error={locks.error} />
        ) : (
          <>
            <LockCounters locks={locks.data} />
            <div className="border-t border-[var(--border-subtle)]">
              <LockTable locks={locks.data} />
            </div>
          </>
        )}
      </Panel>
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
      <dt className="text-muted text-[10px] tracking-wide uppercase">
        {label}
      </dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}
