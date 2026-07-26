/**
 * The header: which database, how verbose, and is the engine alive.
 *
 * The milestone badge and the feature flags come from `/health`, so panels for
 * features that do not exist are never shown. Nothing here pretends to work.
 */

import { useState, type FormEvent } from "react";
import { Badge, Button } from "@/components/primitives";
import {
  useCreateDatabase,
  useDatabases,
  useHealth,
  useSetTraceLevel,
} from "@/hooks/useEngine";
import { TRACE_LEVELS, type TraceLevelName } from "@/lib/api";
import { cn, formatBytes } from "@/lib/format";
import { useTheme } from "@/hooks/useTheme";

const DEFAULT_PAGE_SIZE = 4096;
const PAGE_SIZE_CHOICES = [256, 512, 1024, 4096, 8192];

export function TopBar({
  databaseId,
  onSelectDatabase,
  traceLevel,
}: {
  databaseId: string | null;
  onSelectDatabase: (id: string | null) => void;
  traceLevel: TraceLevelName;
}) {
  const health = useHealth();
  const databases = useDatabases();
  const setTrace = useSetTraceLevel(databaseId ?? "");
  const [theme, toggleTheme] = useTheme();
  const [creating, setCreating] = useState(false);

  const connected = health.isSuccess;

  return (
    <header className="surface flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="text-sm font-bold tracking-tight">ChenDB</span>
        <Badge tone="accent" title="Highest completed milestone in this build">
          M{health.data?.milestone ?? "?"}
        </Badge>
        <span className="text-muted hidden text-[11px] sm:inline">
          Visual Database Explorer
        </span>
      </div>

      <div className="mx-2 h-5 w-px bg-[var(--border-subtle)]" />

      <label className="flex items-center gap-1.5">
        <span className="text-muted text-[10px] tracking-wide uppercase">database</span>
        <select
          aria-label="Select database"
          value={databaseId ?? ""}
          onChange={(event) => onSelectDatabase(event.target.value || null)}
          className="surface-sunken min-w-32 rounded border border-[var(--border-subtle)] px-2 py-1 font-mono text-xs"
        >
          <option value="">— none —</option>
          {databases.data?.databases.map((entry) => (
            <option key={entry.database_id} value={entry.database_id}>
              {entry.database_id} ({formatBytes(entry.size_bytes)})
            </option>
          ))}
        </select>
      </label>

      <Button onClick={() => setCreating((open) => !open)} aria-expanded={creating}>
        + New
      </Button>

      <div className="mx-2 h-5 w-px bg-[var(--border-subtle)]" />

      <label className="flex items-center gap-1.5">
        <span
          className="text-muted text-[10px] tracking-wide uppercase"
          title="How much internal detail the engine reports. Higher levels cost more; the engine runs identically at every level."
        >
          trace
        </span>
        <select
          aria-label="Trace level"
          value={traceLevel}
          disabled={!databaseId}
          onChange={(event) => setTrace.mutate(event.target.value as TraceLevelName)}
          className="surface-sunken rounded border border-[var(--border-subtle)] px-2 py-1 font-mono text-xs disabled:opacity-40"
        >
          {TRACE_LEVELS.map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
      </label>

      <div className="flex-1" />

      <span
        className={cn(
          "flex items-center gap-1.5 rounded px-2 py-1 text-[11px]",
          connected
            ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
            : "bg-red-500/15 text-red-600 dark:text-red-300",
        )}
        role="status"
      >
        <span
          aria-hidden
          className={cn(
            "size-1.5 rounded-full",
            connected ? "bg-emerald-500" : "bg-red-500",
          )}
        />
        {connected ? `engine ${health.data.engine_version}` : "disconnected"}
      </span>

      <Button
        onClick={toggleTheme}
        aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      >
        {theme === "dark" ? "☀" : "☾"}
      </Button>

      {creating ? (
        <CreateDatabaseForm
          onDone={(id) => {
            setCreating(false);
            if (id) onSelectDatabase(id);
          }}
        />
      ) : null}
    </header>
  );
}

function CreateDatabaseForm({ onDone }: { onDone: (id: string | null) => void }) {
  const [databaseId, setDatabaseId] = useState("demo");
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const create = useCreateDatabase();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate(
      { database_id: databaseId, page_size: pageSize },
      { onSuccess: (created) => onDone(created.database_id) },
    );
  };

  return (
    <form
      onSubmit={submit}
      className="surface flex w-full flex-wrap items-end gap-2 rounded border p-2"
    >
      <label className="block">
        <span className="text-muted text-[10px] tracking-wide uppercase">name</span>
        <input
          autoFocus
          value={databaseId}
          onChange={(event) => setDatabaseId(event.target.value)}
          pattern="[A-Za-z0-9][A-Za-z0-9._\-]*"
          maxLength={64}
          required
          className="surface-sunken block rounded border border-[var(--border-subtle)] px-2 py-1 font-mono text-xs"
        />
      </label>
      <label className="block">
        <span
          className="text-muted text-[10px] tracking-wide uppercase"
          title="Small pages fill after a handful of rows, which makes page chaining easy to watch."
        >
          page size
        </span>
        <select
          value={pageSize}
          onChange={(event) => setPageSize(Number(event.target.value))}
          className="surface-sunken block rounded border border-[var(--border-subtle)] px-2 py-1 font-mono text-xs"
        >
          {PAGE_SIZE_CHOICES.map((size) => (
            <option key={size} value={size}>
              {size} B{size === 256 ? " (demo)" : ""}
            </option>
          ))}
        </select>
      </label>
      <Button type="submit" variant="primary" disabled={create.isPending}>
        Create
      </Button>
      <Button onClick={() => onDone(null)}>Cancel</Button>
      {create.isError ? (
        <span role="alert" className="text-[11px] text-red-600 dark:text-red-400">
          {(create.error as Error).message}
        </span>
      ) : null}
    </form>
  );
}
