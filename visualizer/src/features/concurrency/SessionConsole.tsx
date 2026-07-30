/**
 * One console: one session, one transaction, one view of the database.
 *
 *   ┌─ alice ──────────────── read committed ── #7 active ──┐
 *   │ [BEGIN] [COMMIT] [ROLLBACK]     snapshot xmin=7 xmax=9│
 *   │ SELECT * FROM users;                          [ Run ] │
 *   ├───────────────────────────────────────────────────────┤
 *   │ id  label                                             │
 *   │  1  ada                                               │
 *   └───────────────────────────────────────────────────────┘
 *
 * Two of these side by side is the whole demonstration. They talk to the same
 * database through the same API and differ in exactly one thing: the
 * ``?session=`` on every request. That is enough for them to hold separate
 * transactions, see different rows, and block each other.
 *
 * The snapshot is printed in the header rather than hidden in a panel, because
 * "why can't I see the row the other console just inserted" is the question
 * this whole view exists to answer, and ``xmin=7 xmax=9 active={7}`` is the
 * answer.
 */

import { useState } from "react";
import { Badge, Button, ErrorNotice, Panel } from "@/components/primitives";
import {
  useRunQuery,
  useTransactionAction,
  useTransactions,
} from "@/hooks/useEngine";
import { cn, formatValue } from "@/lib/format";
import type { QueryResultModel, SessionModel } from "@/types/api";

export function SessionConsole({
  databaseId,
  session,
  info,
  defaultSql,
}: {
  databaseId: string;
  session: string;
  info: SessionModel | undefined;
  defaultSql: string;
}) {
  const [sql, setSql] = useState(defaultSql);
  const [results, setResults] = useState<QueryResultModel[] | null>(null);

  const transactions = useTransactions(databaseId, { session });
  const run = useRunQuery(databaseId, session);
  const act = useTransactionAction(databaseId, session);

  const open = transactions.data?.in_explicit_transaction ?? false;
  const busy = run.isPending || act.isPending;
  // `waiting_for` is optional on the wire (Pydantic gives it a default), so a
  // session that has never blocked may not carry the field at all.
  const waitingFor = info?.waiting_for ?? [];

  return (
    <Panel
      className="h-full"
      title={session}
      subtitle={
        info?.transaction_id
          ? `#${info.transaction_id} · ${info.isolation_level ?? ""}`
          : "no transaction"
      }
      actions={
        <div className="flex items-center gap-1.5">
          <Button
            variant="primary"
            disabled={busy || open}
            onClick={() => act.mutate("begin")}
          >
            BEGIN
          </Button>
          <Button disabled={busy || !open} onClick={() => act.mutate("commit")}>
            COMMIT
          </Button>
          <Button
            variant="danger"
            disabled={busy || !open}
            onClick={() => act.mutate("rollback")}
          >
            ROLLBACK
          </Button>
        </div>
      }
    >
      <div className="flex min-h-0 flex-col">
        <div className="space-y-1 border-b border-[var(--border-subtle)] px-3 py-2 font-mono text-[11px]">
          {info?.snapshot ? (
            <p
              className="text-muted"
              title="The set of transactions this session can see"
            >
              snapshot {info.snapshot}
            </p>
          ) : (
            <p className="text-muted">no snapshot; nothing read yet</p>
          )}
          <p className="text-muted flex flex-wrap gap-x-3">
            <span>{info?.statements ?? 0} stmt</span>
            <span title="One per transaction under repeatable read, one per statement under read committed">
              {info?.snapshots_taken ?? 0} snapshot(s)
            </span>
            <span>{info?.locks_held ?? 0} lock(s)</span>
            {waitingFor.length > 0 ? (
              <Badge tone="danger">
                waiting on {waitingFor.map((t) => `#${t}`).join(", ")}
              </Badge>
            ) : null}
          </p>
        </div>

        <div className="space-y-2 p-3">
          <textarea
            value={sql}
            onChange={(event) => setSql(event.target.value)}
            spellCheck={false}
            rows={3}
            aria-label={`SQL for ${session}`}
            className={cn(
              "w-full resize-y rounded border border-[var(--border-subtle)]",
              "bg-[var(--surface-sunken)] px-2 py-1.5 font-mono text-[11px]",
              "focus:outline-none focus:ring-1 focus:ring-[var(--accent)]",
            )}
          />
          <Button
            variant="primary"
            disabled={busy || !sql.trim()}
            onClick={() =>
              run.mutate({ sql }, { onSuccess: (data) => setResults(data) })
            }
          >
            Run as {session}
          </Button>
          {run.isError ? <ErrorNotice error={run.error} /> : null}
          {act.isError ? <ErrorNotice error={act.error} /> : null}
          {act.data ? (
            <p className="text-muted font-mono text-[11px]">
              {act.data.message}
            </p>
          ) : null}
        </div>

        <div className="min-h-0 flex-1 overflow-auto border-t border-[var(--border-subtle)]">
          <Results results={results} />
        </div>
      </div>
    </Panel>
  );
}

function Results({ results }: { results: QueryResultModel[] | null }) {
  if (!results) {
    return (
      <p className="text-muted p-3 text-[11px]">
        Run something. Whatever comes back is what <em>this</em> session can
        see, which is not necessarily what the other one can.
      </p>
    );
  }

  const last = results[results.length - 1];
  if (!last?.returns_rows) {
    return (
      <p className="p-3 font-mono text-[11px]">{last?.message || "done"}</p>
    );
  }

  return (
    <table className="w-full border-collapse font-mono text-[11px]">
      <thead className="bg-[var(--surface-sunken)] sticky top-0">
        <tr className="text-muted text-left">
          {last.columns.map((column) => (
            <th key={column.name} className="px-2 py-1 font-medium">
              {column.name}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {last.rows.map((row, index) => (
          <tr key={index} className="border-t border-[var(--border-subtle)]">
            {row.map((value, column) => (
              <td key={column} className="px-2 py-1">
                {formatValue(value)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
