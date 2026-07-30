/**
 * Print every demo statement the visualizer can run, as JSON, for the guard in
 * `tests/integration/test_demo_sql.py` to execute against a real engine.
 *
 *     node scripts/emit_demo_sql.ts
 *
 * Run under plain Node, which strips TypeScript types but does not resolve
 * TypeScript's extensionless imports, hence the `.ts` below. Nothing is
 * generated to disk and nothing is committed: the test invokes this, so the
 * catalogue it checks is the one the app actually ships. A generated file would
 * be one more thing that can be stale, and the whole point of this guard is
 * that a stale demo is exactly the bug it exists to catch.
 *
 * The fixture table below is what the guard creates before running the
 * statements. Its shape is deliberate: a primary key, a NOT NULL column (so the
 * "break it half-way" demo has a constraint to violate), a nullable one, and a
 * BOOLEAN, so `literalFor` is exercised across every branch. It is called
 * `users` because the editor's examples name that table directly.
 */

import { demoStatements } from "../visualizer/src/lib/demoSql.ts";
import type { TableDetail } from "../visualizer/src/types/api.ts";

const FIXTURE: TableDetail = {
  table_id: 100,
  name: "users",
  is_system: false,
  schema: { columns: [], fixed_width: false, byte_size: null } as never,
  columns: [
    { name: "id", type: "INTEGER", nullable: false, primary_key: true, fixed_size: 8 },
    { name: "email", type: "TEXT", nullable: false, primary_key: false, fixed_size: null },
    { name: "age", type: "INTEGER", nullable: true, primary_key: false, fixed_size: 8 },
    { name: "active", type: "BOOLEAN", nullable: true, primary_key: false, fixed_size: 1 },
  ],
  storage: {} as never,
};

process.stdout.write(
  JSON.stringify(
    {
      table: {
        name: FIXTURE.name,
        columns: FIXTURE.columns.map((column) => ({
          name: column.name,
          type: column.type,
          nullable: column.nullable,
          primary_key: column.primary_key,
        })),
      },
      statements: demoStatements(FIXTURE),
    },
    null,
    2,
  ),
);
