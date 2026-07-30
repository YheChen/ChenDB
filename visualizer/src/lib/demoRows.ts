/**
 * Building demonstration SQL against a table whose schema you do not know.
 *
 * Every "try it" button in the explorer writes real rows into whatever table
 * happens to be there, and the tables are the user's. Hardcoding
 * `VALUES (1, 'x')` works right up until someone's table has three columns,
 * and then the button reports `row has 2 values but 3 columns`, which is the
 * demonstration failing for a reason that has nothing to do with what it was
 * demonstrating. This module exists because that has now happened twice.
 */

import type { ColumnModel, TableDetail } from "@/types/api";

/** A literal of the right type for a column, varied by seed so keys differ. */
export function literalFor(column: ColumnModel, seed: number): string {
  switch (column.type) {
    case "INTEGER":
      return String(seed);
    case "FLOAT":
      return `${seed}.5`;
    case "BOOLEAN":
      return seed % 2 === 0 ? "TRUE" : "FALSE";
    default:
      return `'demo-${seed}'`;
  }
}

/**
 * One `INSERT` covering every column, with `nullIn` set to NULL if given.
 *
 * The NULL is how a demonstration causes a failure *during* the insert rather
 * than before it, which is the only kind of failure that leaves earlier rows
 * for an undo log to take back.
 */
export function insertRow(
  table: TableDetail,
  seed: number,
  nullIn?: string,
): string {
  const values = table.columns
    .map((column) =>
      column.name === nullIn ? "NULL" : literalFor(column, seed),
    )
    .join(", ");
  return `INSERT INTO ${table.name} VALUES (${values});`;
}

/** ``count`` inserts, seeds counting up from ``from``. */
export function insertRows(
  table: TableDetail,
  { from, count }: { from: number; count: number },
): string {
  return Array.from({ length: count }, (_, i) =>
    insertRow(table, from + i),
  ).join("\n");
}

/**
 * A column a NULL is not allowed in, or undefined if every column is nullable.
 *
 * NOT NULL is the one constraint ChenDB enforces on every table without an
 * index. A duplicate key would be the more familiar way to make an insert fail,
 * but `PRIMARY KEY` here is metadata and uniqueness comes from a UNIQUE index
 * the table may not have, so a duplicate-key demonstration would silently
 * succeed, which is worse than not offering it.
 */
export function notNullColumn(table: TableDetail): ColumnModel | undefined {
  return table.columns.find((column) => !column.nullable);
}
