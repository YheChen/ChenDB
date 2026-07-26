/**
 * Schema definition and row insertion.
 *
 * Both forms are Milestone 1 stand-ins for SQL. They are labelled as such
 * rather than dressed up as a query editor, because a SQL box that only accepts
 * two shapes of statement would be worse than no SQL box at all.
 */

import { useState, type FormEvent } from "react";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNotice,
  Field,
  Panel,
  Spinner,
} from "@/components/primitives";
import { useCreateTable, useInsertRecords, useTable } from "@/hooks/useEngine";
import { formatBytes, formatCount } from "@/lib/format";
import type { ColumnSpec, TableResponse } from "@/types/api";

const SQL_TYPES = ["INTEGER", "TEXT", "FLOAT", "BOOLEAN"] as const;

const STARTER_COLUMNS: ColumnSpec[] = [
  { name: "id", type: "INTEGER", nullable: false, primary_key: true },
  { name: "email", type: "TEXT", nullable: false, primary_key: false },
  { name: "age", type: "INTEGER", nullable: true, primary_key: false },
];

export function SchemaPanel({
  databaseId,
  onTableChange,
}: {
  databaseId: string | null;
  onTableChange: (table: TableResponse | null) => void;
}) {
  const query = useTable(databaseId);

  if (!databaseId) {
    return (
      <Panel title="Schema" className="h-full">
        <EmptyState title="No database open" hint="Create one to get started." />
      </Panel>
    );
  }

  if (query.isPending) {
    return (
      <Panel title="Schema" className="h-full">
        <Spinner label="Reading catalog" />
      </Panel>
    );
  }

  // A 404 here means "no table yet" — an expected state, not a failure.
  if (query.isError) {
    const notFound =
      typeof query.error === "object" &&
      query.error !== null &&
      "status" in query.error &&
      (query.error as { status: number }).status === 404;
    if (!notFound) {
      return (
        <Panel title="Schema" className="h-full">
          <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
        </Panel>
      );
    }
    return (
      <Panel title="Schema" subtitle="no table yet" className="h-full">
        <CreateTableForm databaseId={databaseId} onCreated={onTableChange} />
      </Panel>
    );
  }

  return (
    <Panel
      title="Schema"
      subtitle={`${query.data.name} · ${formatCount(query.data.row_count)} rows`}
      className="h-full"
    >
      <TableDetail table={query.data} databaseId={databaseId} />
    </Panel>
  );
}

function TableDetail({
  table,
  databaseId,
}: {
  table: TableResponse;
  databaseId: string;
}) {
  return (
    <div className="space-y-3 p-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5">
        <Field label="rows" value={formatCount(table.row_count)} />
        <Field label="heap pages" value={table.heap_page_ids.length} />
        <Field
          label="null bitmap"
          value={formatBytes(table.schema.null_bitmap_size)}
          title="One bit per column, rounded up to whole bytes. Present on every record."
        />
        <Field
          label="row size"
          value={
            table.schema.fixed_row_size === null
              ? "variable"
              : `${table.schema.fixed_row_size} B fixed`
          }
          title="A schema with only fixed-width columns has a constant row size, which makes column offsets computable rather than requiring a walk."
        />
      </dl>

      <table className="w-full text-left text-xs">
        <thead className="surface-sunken text-muted">
          <tr>
            <th className="px-2 py-1 font-medium">#</th>
            <th className="px-2 py-1 font-medium">column</th>
            <th className="px-2 py-1 font-medium">type</th>
            <th className="px-2 py-1 font-medium">width</th>
            <th className="px-2 py-1 font-medium">flags</th>
          </tr>
        </thead>
        <tbody>
          {table.schema.columns.map((column, index) => (
            <tr key={column.name} className="border-t border-[var(--border-subtle)]">
              <td className="text-muted px-2 py-1 font-mono text-[10px]">{index}</td>
              <td className="px-2 py-1 font-medium">{column.name}</td>
              <td className="px-2 py-1 font-mono text-[11px]">{column.type}</td>
              <td className="text-muted px-2 py-1 font-mono text-[11px]">
                {column.fixed_size === null ? "var" : `${column.fixed_size} B`}
              </td>
              <td className="px-2 py-1">
                <span className="flex gap-1">
                  {column.primary_key ? <Badge tone="accent">PK</Badge> : null}
                  {!column.nullable ? <Badge tone="neutral">NOT NULL</Badge> : null}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <InsertRowForm databaseId={databaseId} table={table} />
    </div>
  );
}

function CreateTableForm({
  databaseId,
  onCreated,
}: {
  databaseId: string;
  onCreated: (table: TableResponse) => void;
}) {
  const [columns, setColumns] = useState<ColumnSpec[]>(STARTER_COLUMNS);
  const [name, setName] = useState("users");
  const create = useCreateTable(databaseId);

  const update = (index: number, patch: Partial<ColumnSpec>) => {
    setColumns((current) =>
      current.map((column, position) =>
        position === index ? { ...column, ...patch } : column,
      ),
    );
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate({ name, columns }, { onSuccess: onCreated });
  };

  return (
    <form onSubmit={submit} className="space-y-3 p-3">
      <p className="text-muted text-[11px]">
        Milestone 1 has no SQL parser, so tables are defined structurally.
        Milestone 2 replaces this form with <code>CREATE TABLE</code>.
      </p>

      <label className="block">
        <span className="text-muted text-[10px] tracking-wide uppercase">
          table name
        </span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          maxLength={64}
          className="surface-sunken mt-0.5 w-full rounded border border-[var(--border-subtle)] px-2 py-1 font-mono text-xs"
        />
      </label>

      <div className="space-y-1.5">
        <span className="text-muted text-[10px] tracking-wide uppercase">columns</span>
        {columns.map((column, index) => (
          <div key={index} className="flex flex-wrap items-center gap-1.5">
            <input
              aria-label={`Column ${index + 1} name`}
              value={column.name}
              onChange={(event) => update(index, { name: event.target.value })}
              required
              className="surface-sunken w-28 rounded border border-[var(--border-subtle)] px-2 py-1 font-mono text-xs"
            />
            <select
              aria-label={`Column ${index + 1} type`}
              value={column.type}
              onChange={(event) =>
                update(index, { type: event.target.value as ColumnSpec["type"] })
              }
              className="surface-sunken rounded border border-[var(--border-subtle)] px-1.5 py-1 text-xs"
            >
              {SQL_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
            <label className="text-muted flex items-center gap-1 text-[11px]">
              <input
                type="checkbox"
                checked={!column.nullable}
                onChange={(event) => update(index, { nullable: !event.target.checked })}
              />
              NOT NULL
            </label>
            <label className="text-muted flex items-center gap-1 text-[11px]">
              <input
                type="checkbox"
                checked={column.primary_key}
                onChange={(event) =>
                  update(index, {
                    primary_key: event.target.checked,
                    nullable: event.target.checked ? false : column.nullable,
                  })
                }
              />
              PK
            </label>
            <button
              type="button"
              aria-label={`Remove column ${index + 1}`}
              onClick={() =>
                setColumns((current) => current.filter((_, i) => i !== index))
              }
              disabled={columns.length === 1}
              className="text-muted px-1 hover:text-red-600 disabled:opacity-30"
            >
              ×
            </button>
          </div>
        ))}
        <Button
          onClick={() =>
            setColumns((current) => [
              ...current,
              {
                name: `col${current.length + 1}`,
                type: "TEXT",
                nullable: true,
                primary_key: false,
              },
            ])
          }
        >
          + Add column
        </Button>
      </div>

      {create.isError ? (
        <p role="alert" className="text-[11px] text-red-600 dark:text-red-400">
          {(create.error as Error).message}
        </p>
      ) : null}

      <Button type="submit" variant="primary" disabled={create.isPending}>
        {create.isPending ? "Creating…" : "Create table"}
      </Button>
    </form>
  );
}

function InsertRowForm({
  databaseId,
  table,
}: {
  databaseId: string;
  table: TableResponse;
}) {
  const [values, setValues] = useState<string[]>(() =>
    table.schema.columns.map(() => ""),
  );
  const insert = useInsertRecords(databaseId);

  /** Convert a text input into the JSON type the column expects. */
  const coerce = (raw: string, index: number): unknown => {
    const column = table.schema.columns[index];
    if (!column) return raw;
    if (raw === "" || raw.toUpperCase() === "NULL") return null;
    switch (column.type) {
      case "INTEGER":
        return Number.parseInt(raw, 10);
      case "FLOAT":
        return Number.parseFloat(raw);
      case "BOOLEAN":
        return ["true", "t", "yes", "1"].includes(raw.toLowerCase());
      default:
        return raw;
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    insert.mutate([values.map(coerce)], {
      onSuccess: () => setValues(table.schema.columns.map(() => "")),
    });
  };

  return (
    <form onSubmit={submit} className="space-y-2 border-t border-[var(--border-subtle)] pt-3">
      <span className="text-muted text-[10px] tracking-wide uppercase">insert a row</span>
      <div className="grid grid-cols-2 gap-1.5">
        {table.schema.columns.map((column, index) => (
          <label key={column.name} className="block">
            <span className="text-muted text-[10px]">
              {column.name}
              <span className="ml-1 font-mono opacity-70">{column.type}</span>
            </span>
            <input
              value={values[index] ?? ""}
              onChange={(event) =>
                setValues((current) =>
                  current.map((value, position) =>
                    position === index ? event.target.value : value,
                  ),
                )
              }
              placeholder={column.nullable ? "NULL" : "required"}
              className="surface-sunken w-full rounded border border-[var(--border-subtle)] px-2 py-1 font-mono text-xs"
            />
          </label>
        ))}
      </div>

      {insert.isError ? (
        <p role="alert" className="text-[11px] text-red-600 dark:text-red-400">
          {(insert.error as Error).message}
        </p>
      ) : null}

      <Button type="submit" variant="primary" disabled={insert.isPending}>
        {insert.isPending ? "Inserting…" : "Insert row"}
      </Button>
    </form>
  );
}
