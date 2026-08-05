/**
 * Every piece of SQL the explorer will run on the user's behalf, in one place.
 *
 * The buttons scattered through the workspaces, walkthroughs, workloads,
 * "break it half-way", the editor's examples, all write real SQL against the
 * user's real tables. Nothing checked that any of it was valid, and twice it
 * was not:
 *
 *   Milestone  8  `INSERT INTO t VALUES (1, 'x')`   → row has 2 values but 3 columns
 *   Milestone 10  `DELETE FROM t WHERE id = 1`      → DELETE is not implemented yet
 *
 * The second one shipped and sat in the UI for a whole milestone, because a
 * demo button is exactly the code nobody runs in a test. Collecting the
 * statements here makes them *enumerable*, and
 * `tests/integration/test_demo_sql.py` runs every one of them through the real
 * engine and asserts it does what this file says it does.
 *
 * Two rules for anything added here:
 *
 *   1. Build it from the table that is open, never from a guessed column name.
 *      `demoRows.ts` exists for that.
 *   2. Say honestly what it should do. A demo that is *supposed* to fail,
 *      "break it half-way", the editor's syntax-error example, is as much a
 *      claim as one that works, and goes stale the same way when the engine
 *      changes underneath it.
 *
 * The `.ts` on the import below is deliberate: `scripts/emit_demo_sql.ts` runs
 * this module under plain Node, which strips types but does not resolve
 * TypeScript's extensionless imports. Type-only imports are erased outright,
 * so the `@/` alias above is fine.
 */

import type { TableDetail } from "@/types/api";
import { insertRow, insertRows, literalFor, notNullColumn } from "./demoRows.ts";

export type DemoStatement = {
  /** `workspace/demo[/console]`, stable enough to name in a failure message. */
  id: string;
  label: string;
  sql: string;
  /**
   * Whether the parser must accept it. Checked for every statement, and the
   * assertion that would have caught `DELETE FROM t WHERE id = 1` shipping
   * against a parser that refused `DELETE`.
   */
  parses: boolean;
  /**
   * What executing it must do:
   *
   *   `"ok"`: a button runs this, and it must succeed. The assertion that
   *               would have caught the two-value `INSERT` into a three-column
   *               table.
   *   `"error"`: a button runs this and it is *meant* to fail. "Break it
   *               half-way" has no point if the last row succeeds.
   *   `"skip"`: never run by a button. The editor's examples illustrate
   *               syntax against whatever tables the user happens to have, so
   *               whether they execute is not this file's claim to make.
   */
  runs: "ok" | "error" | "skip";
  /** `"seeded"`, the fixture table, with rows. `"empty"`, a fresh database. */
  on: "seeded" | "empty";
};

export type Demo<T> = T & { id: string; label: string; hint: string };

// --------------------------------------------------------------------------
// Buffer pool, workloads
// --------------------------------------------------------------------------

export type Workload = Demo<{ sql: (table: string) => string }>;

export const WORKLOADS: Workload[] = [
  {
    id: "scan",
    label: "Scan once",
    hint: "Reads every page. If the table is bigger than the pool, this evicts everything as it goes, and gains nothing from having done so.",
    sql: (table) => `SELECT * FROM ${table};`,
  },
  {
    id: "scan-twice",
    label: "Scan twice",
    hint: "The second pass hits every page, if the table fits. If it does not, the hit rate barely moves: that is sequential flooding.",
    sql: (table) => `SELECT * FROM ${table};\nSELECT * FROM ${table};`,
  },
  {
    id: "repeat",
    label: "Scan ten times",
    hint: "A working set that fits is loaded once and served nine times from memory.",
    sql: (table) =>
      Array.from({ length: 10 }, () => `SELECT * FROM ${table};`).join("\n"),
  },
];

// --------------------------------------------------------------------------
// Transactions, rollback demonstrations
// --------------------------------------------------------------------------

export type TransactionDemo = Demo<{
  /** Built from the table's real schema, so it works whatever the columns are. */
  sql: (table: TableDetail) => string;
  /** Why this demo cannot run against this table, or null when it can. */
  blockedBy?: (table: TableDetail) => string | null;
  /** True when the statement is supposed to fail, that *is* the demonstration. */
  failsOnPurpose?: boolean;
}>;

export const TRANSACTION_DEMOS: TransactionDemo[] = [
  {
    id: "half-way",
    label: "Break it half-way",
    hint: "Two good rows, then one with NULL in a NOT NULL column. The first two really are written (watch the undo log grow) and then taken back.",
    failsOnPurpose: true,
    blockedBy: (table) =>
      notNullColumn(table)
        ? null
        : `Every column of ${table.name} is nullable, so there is no constraint here to violate.`,
    sql: (table) => {
      const doomed = notNullColumn(table)!;
      return [
        insertRow(table, 910_001),
        insertRow(table, 910_002),
        insertRow(table, 910_003, doomed.name),
      ].join("\n");
    },
  },
  {
    id: "ddl",
    label: "Create a table, then roll back",
    hint: "CREATE TABLE writes rows into two system tables and allocates a heap page. The undo log works in pages, so it takes all of that back without knowing what any of it meant. Roll back and watch the table vanish from Storage.",
    sql: () =>
      "BEGIN;\nCREATE TABLE rolled_back_demo (id INTEGER PRIMARY KEY, note TEXT);",
  },
];

// --------------------------------------------------------------------------
// WAL, durability demonstrations
// --------------------------------------------------------------------------

export type WalDemo = Demo<{
  sql: (table: TableDetail) => string;
  note: string;
  /** True for the one that must not commit, the crash button undoes it. */
  requiresNoTransaction?: boolean;
}>;

export const WAL_DEMOS: WalDemo[] = [
  {
    id: "commit-twenty",
    label: "Commit twenty rows",
    hint: "Each statement commits, so each one fsyncs the log. Watch the record count and the log size climb.",
    note: "Twenty committed rows. Each statement got an implicit transaction, so each one appended a commit record and fsynced, which is why the sync count went up by twenty.",
    sql: (table) => insertRows(table, { from: 800_000, count: 20 }),
  },
  {
    id: "uncommitted-fifty",
    label: "Write fifty uncommitted rows",
    hint: "Open a transaction and write into it without committing. Then crash, and watch these rows not come back.",
    note: "Fifty rows inside an open transaction, with no commit. They are in the table now and in the log, but with no commit record. Crash the database and recovery will take them back.",
    requiresNoTransaction: true,
    sql: (table) => `BEGIN;\n${insertRows(table, { from: 900_000, count: 50 })}`,
  },
];

// --------------------------------------------------------------------------
// MVCC, two consoles, one database
// --------------------------------------------------------------------------

export type Walkthrough = Demo<{ alice: string; bob: string }>;

/**
 * A column worth writing a demonstration value into.
 *
 * Not the primary key if anything else is available: setting every row's `id`
 * to the same number makes the table nonsense, and a reader trying to follow
 * the version chain then cannot tell the rows apart.
 */
export function updateTarget(table: TableDetail) {
  return table.columns.find((column) => !column.primary_key) ?? table.columns[0];
}

export function walkthroughs(table: TableDetail): Walkthrough[] {
  const name = table.name;
  const list: Walkthrough[] = [
    {
      id: "invisible",
      label: "A reader does not wait",
      hint: "Run bob's BEGIN and INSERT, then alice's SELECT. Alice returns immediately and does not see the row: she read an older version rather than waiting for the newer one. Then commit bob and run alice again.",
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
  // exists to prevent.
  const target = updateTarget(table);
  if (target) {
    list.push(
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
        hint: "BEGIN bob and run his UPDATE without committing. The lock table below fills with one exclusive lock per version he touched, two per row: the old one and the new. Now run alice's SELECT: it returns instantly, holds 0 locks, and “readers blocked” stays 0. That is the whole claim of MVCC, in one panel.",
        alice: `SELECT * FROM ${name};`,
        bob: `UPDATE ${name} SET ${target.name} = ${literalFor(target, 200)};`,
      },
      {
        id: "rollback",
        label: "A rollback leaves nothing behind",
        hint: "BEGIN bob, run his UPDATE, and check rows vs versions on the Storage tab: rows unchanged, versions up by one per row. Now ROLLBACK and look again: the new versions are gone, not merely dead, and Vacuum has nothing to do. ChenDB undoes by restoring pages, which is why it needs no commit log; PostgreSQL, which does not undo, needs both CLOG and a vacuum pass for the rows an aborted transaction left.",
        alice: `SELECT * FROM ${name};`,
        bob: `UPDATE ${name} SET ${target.name} = ${literalFor(target, 300)};`,
      },
    );
  }
  return list;
}

// --------------------------------------------------------------------------
// Fixed text: the editor's examples and the execution workspace's opener
// --------------------------------------------------------------------------

export type Example = { label: string; sql: string; parses: boolean };

export const JOIN_DEMO_SQL = `-- Two tables, one join, and a planner with something to decide.
-- Run it, then read the plan: the ON condition became a HashJoin, the WHERE
-- was pushed BELOW it, and the smaller table is the one being hashed.
--
-- IF NOT EXISTS, so running it twice works. The rows accumulate, which is
-- itself worth watching: the statistics go stale and the plan can flip.

CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY, customer_id INTEGER, amount INTEGER);

INSERT INTO customers VALUES
  (1, 'ada', 'london'), (2, 'alan', 'london'),
  (3, 'grace', 'new york'), (4, 'edsger', 'amsterdam');

INSERT INTO sales VALUES
  (10, 1, 120), (11, 1,  80), (12, 2, 300),
  (13, 3, 45),  (14, 3, 220), (15, 4,  15), (16, 99, 5);

ANALYZE;

SELECT c.city, COUNT(*) AS orders, SUM(s.amount) AS revenue
FROM customers c JOIN sales s ON c.id = s.customer_id
WHERE s.amount > 20
GROUP BY c.city
HAVING SUM(s.amount) > 100
ORDER BY revenue DESC
LIMIT 5;`;

export const OUTER_JOIN_DEMO_SQL = `-- An outer join keeps what an inner one throws away.
-- Run it and compare the two results: the LEFT JOIN keeps 'edsger', who has no
-- sales at all, with NULLs where the sale would have been.
--
-- Then read the plans. The ON condition stays AT the join for the outer one,
-- for an inner join the planner would push it below, and for an outer join that
-- would be wrong: it would filter the rows the join exists to preserve.

CREATE TABLE IF NOT EXISTS staff (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shifts (id INTEGER PRIMARY KEY, staff_id INTEGER, hours INTEGER);

INSERT INTO staff VALUES (1, 'ada'), (2, 'alan'), (3, 'grace'), (4, 'edsger');
INSERT INTO shifts VALUES (10, 1, 8), (11, 1, 4), (12, 2, 9), (13, 99, 6);

-- Every member of staff, with their shifts if they have any.
SELECT s.name, sh.hours
FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id
ORDER BY s.name, sh.hours;

-- The anti-join idiom: who has no shifts at all. Only an outer join can ask
-- this, because the row you are looking for is the one that did not match.
SELECT s.name
FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id
WHERE sh.id IS NULL
ORDER BY s.name;

-- And a FULL join, which also keeps shift 13, whose staff_id matches nobody.
SELECT s.name, sh.id
FROM staff s FULL JOIN shifts sh ON s.id = sh.staff_id
ORDER BY s.name, sh.id;`;

export const SIMPLIFIED_OUTER_JOIN_DEMO_SQL = `-- An outer join is not always an outer join.
-- Run this, then read the plan panel for each SELECT. Two of them say
-- "rewrites applied: simplify_outer_joins" and run as inner joins, and one does
-- not. The difference is what the WHERE could possibly say about a row the join
-- invented by filling the missing side with NULLs.

CREATE TABLE IF NOT EXISTS staff (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shifts (id INTEGER PRIMARY KEY, staff_id INTEGER, hours INTEGER);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, shift_id INTEGER, body TEXT);

INSERT INTO staff VALUES (1, 'ada'), (2, 'alan'), (3, 'grace'), (4, 'edsger');
INSERT INTO shifts VALUES (10, 1, 8), (11, 1, 4), (12, 2, 9), (13, 99, 6);
INSERT INTO notes VALUES (20, 10, 'late'), (21, 12, 'swap');

-- edsger has no shifts, so this join preserves a row whose sh.hours is NULL.
-- NULL > 5 is NULL, a WHERE keeps only TRUE, and that row cannot survive. So
-- the planner runs an inner join instead, and now that it may, it pushes the
-- filter below the join as well.
SELECT s.name, sh.hours
FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id
WHERE sh.hours > 5
ORDER BY s.name;

-- The same predicate with an escape hatch. NULL > 5 is still NULL, but
-- sh.hours IS NULL is TRUE about exactly the invented rows, and one survivable
-- branch of an OR is enough. The join stays outer and edsger comes back.
SELECT s.name, sh.hours
FROM staff s LEFT JOIN shifts sh ON s.id = sh.staff_id
WHERE sh.hours > 5 OR sh.hours IS NULL
ORDER BY s.name;

-- And the case that needs no WHERE at all. The inner join at the bottom throws
-- away any row its ON rejects, and sh.id = n.shift_id is NULL for every invented
-- row, so the outer join above it has nothing left to preserve. Both run as
-- inner joins, and the order search is free to reorder all three tables.
SELECT s.name, n.body
FROM staff s
LEFT JOIN shifts sh ON s.id = sh.staff_id
     JOIN notes  n  ON sh.id = n.shift_id
ORDER BY s.name;`;

/**
 * The database every visitor lands on.
 *
 * The explorer used to open on eight panels all saying "No database open",
 * which is accurate and useless: everything worth looking at needs a database,
 * and a first-time visitor has no reason to know that `+ New` is the way in.
 * So one is created and filled before they arrive.
 *
 * Three choices in here are deliberate:
 *
 * - **256-byte pages.** A handful of rows fills one, so the disk map has a
 *   dozen pages to look at rather than one, and the heap chain is visible
 *   growing. It is the size the create dialog labels "(demo)" for this reason.
 * - **An index on a non-key column.** `users_age` gives the B+ tree view a real
 *   tree with a real height, and gives the planner two access paths to choose
 *   between, which is the whole of the Execution workspace.
 * - **ANALYZE at the end.** Statistics are gathered lazily anyway, but running
 *   it here means the first plan a visitor sees was costed against real numbers
 *   rather than against a first-query guess.
 */
export const SAMPLE_DATABASE_ID = "demo";
export const SAMPLE_PAGE_SIZE = 256;

const CITIES = ["london", "berlin", "lisbon", "oslo", "porto"];

export function sampleDatabaseSql(): string {
  const users = Array.from({ length: 40 }, (_, index) => {
    const city = CITIES[index % CITIES.length];
    // Every seventh age is NULL. Three-valued logic is the thing about SQL that
    // surprises people most, and a demo with no NULLs in it cannot show any.
    const age = index % 7 === 0 ? "NULL" : String(21 + ((index * 5) % 45));
    return `(${index + 1}, 'user${index + 1}@example.com', '${city}', ${age})`;
  });
  const orders = Array.from({ length: 60 }, (_, index) => {
    const user = (index % 40) + 1;
    return `(${100 + index}, ${user}, ${((index * 37) % 500) + 10})`;
  });

  return [
    "-- The database the explorer opens with. Everything here is real: these",
    "-- statements ran against the engine to build the pages you are looking at.",
    "CREATE TABLE users (",
    "  id    INTEGER PRIMARY KEY,",
    "  email TEXT NOT NULL,",
    "  city  TEXT,",
    "  age   INTEGER",
    ");",
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total INTEGER);",
    `INSERT INTO users VALUES\n  ${users.join(",\n  ")};`,
    `INSERT INTO orders VALUES\n  ${orders.join(",\n  ")};`,
    "CREATE INDEX users_age ON users (age);",
    "ANALYZE;",
  ].join("\n");
}


export const EXAMPLES: Example[] = [
  {
    label: "CREATE TABLE",
    parses: true,
    sql: `CREATE TABLE users (
  id     INTEGER PRIMARY KEY,
  email  TEXT NOT NULL,
  age    INTEGER,
  active BOOLEAN
);`,
  },
  {
    label: "INSERT",
    parses: true,
    sql: `INSERT INTO users (id, email, age, active) VALUES
  (1, 'ada@example.com', 36, TRUE),
  (2, 'alan@example.com', NULL, FALSE);`,
  },
  {
    label: "UPDATE and DELETE",
    parses: true,
    sql: `UPDATE users SET age = age + 1 WHERE email = 'ada@example.com';
DELETE FROM users WHERE age IS NULL;`,
  },
  {
    label: "SELECT with WHERE",
    parses: true,
    sql: `SELECT email, age * 2 AS doubled
FROM users
WHERE age >= 18 AND email IS NOT NULL;`,
  },
  {
    label: "Operator precedence",
    parses: true,
    sql: `-- Parses as: a = 1 OR (b = 2 AND c = 3)
SELECT * FROM t WHERE a = 1 OR b = 2 AND c = 3;`,
  },
  {
    label: "Quoted identifier",
    parses: true,
    sql: `-- "select" is reserved; quoting makes it a name
SELECT "select" FROM "order";`,
  },
  {
    label: "A syntax error",
    parses: false,
    sql: `SELECT name FROM`,
  },
  {
    label: "Joins and aggregation",
    parses: true,
    sql: JOIN_DEMO_SQL,
  },
  {
    label: "Outer joins",
    parses: true,
    sql: OUTER_JOIN_DEMO_SQL,
  },
  {
    label: "When an outer join is not one",
    parses: true,
    sql: SIMPLIFIED_OUTER_JOIN_DEMO_SQL,
  },
  {
    label: "Not implemented yet",
    parses: false,
    // Third occupant of this slot. It was `ORDER BY` until Milestone 13, then
    // `LEFT JOIN` until Milestone 18, and both times the guard in
    // tests/integration/test_demo_sql.py failed with "was accepted" on the very
    // next run. That is the failure mode this catalogue exists for, and it has
    // now fired twice for the same reason: an example of what the engine cannot
    // do is a claim with a shelf life.
    sql: `SELECT DISTINCT city FROM users;`,
  },
];

/**
 * What the SQL workspace opens with.
 *
 * That workspace *only* parses, it is the tokens-and-AST view, and its Parse
 * button never touches a database. The comment says so without implying the
 * engine cannot execute, which the previous one did: it read "nothing executes
 * yet (that is Milestone 3)" for thirteen milestones after Milestone 3 shipped,
 * because it lived in a component rather than here and the demo-SQL guard never
 * saw it.
 */
export const SQL_INITIAL_SQL = `-- This view parses; it does not run anything.
-- Click any token or AST node to highlight the SQL it came from, or put the
-- cursor in the SQL to select the innermost node containing it.
--
-- To execute, use the Execution workspace.

SELECT u.email, COUNT(*) AS orders, SUM(o.total) AS spend
FROM users u JOIN orders o ON u.id = o.user_id
WHERE u.age >= 18 AND u.email IS NOT NULL
GROUP BY u.email
HAVING SUM(o.total) > 100
ORDER BY spend DESC
LIMIT 10;`;

/**
 * What the Execution workspace opens with.
 *
 * The two examples that reference `users` before creating it are the editor's
 * (which only parses, never runs). This one really executes, so it builds its
 * own table first.
 */
export const EXECUTION_INITIAL_SQL = `-- ⌘↵ runs this. "Start stepping" walks it one operation at a time.
-- Note alan: a NULL age makes \`age >= 18\` unknown, and unknown is not TRUE,
-- so the filter drops that row.

CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, age INTEGER);

INSERT INTO users VALUES
  (1, 'ada@example.com',   36),
  (2, 'alan@example.com',  NULL),
  (3, 'grace@example.com', 45),
  (4, 'edgar@example.com', 17);

SELECT email, age * 2 AS doubled FROM users WHERE age >= 18;`;

/**
 * A two-table schema and a query that has to choose an order to join it in.
 *
 * Loaded by the Execution workspace's "Joins" button. It builds its own tables
 * because the interesting part is the *plan*, and a plan needs statistics,
 * hence the ANALYZE, without which the planner is guessing and says so.
 */
// --------------------------------------------------------------------------
// The catalogue
// --------------------------------------------------------------------------

/**
 * Every statement above, flattened, for a table with this shape.
 *
 * Statements that reference `users` by name are only meaningful against a
 * table called `users`, so the guard passes one in. Everything else is built
 * from `table` and works whatever its columns are, which is the property
 * being checked as much as the SQL itself.
 */
export function demoStatements(table: TableDetail): DemoStatement[] {
  const out: DemoStatement[] = [];
  const add = (entry: Omit<DemoStatement, "parses" | "runs" | "on"> &
    Partial<DemoStatement>) =>
    out.push({ parses: true, runs: "ok", on: "seeded", ...entry });

  for (const workload of WORKLOADS) {
    add({
      id: `buffer/${workload.id}`,
      label: workload.label,
      sql: workload.sql(table.name),
    });
  }
  for (const demo of TRANSACTION_DEMOS) {
    // A blocked demo's button is disabled and its SQL would not be built,
    // `half-way` needs a NOT NULL column to violate.
    if (demo.blockedBy?.(table)) continue;
    add({
      id: `transactions/${demo.id}`,
      label: demo.label,
      sql: demo.sql(table),
      runs: demo.failsOnPurpose ? "error" : "ok",
    });
  }
  for (const demo of WAL_DEMOS) {
    add({ id: `wal/${demo.id}`, label: demo.label, sql: demo.sql(table) });
  }
  for (const walkthrough of walkthroughs(table)) {
    add({
      id: `mvcc/${walkthrough.id}/alice`,
      label: walkthrough.label,
      sql: walkthrough.alice,
    });
    add({
      id: `mvcc/${walkthrough.id}/bob`,
      label: walkthrough.label,
      sql: walkthrough.bob,
    });
  }
  for (const example of EXAMPLES) {
    add({
      id: `editor/${example.label}`,
      label: example.label,
      sql: example.sql,
      parses: example.parses,
      runs: "skip",
    });
  }
  add({
    id: "sql/initial",
    label: "SQL workspace opener",
    sql: SQL_INITIAL_SQL,
    // Parsed, never run: that workspace has no database behind it, and the
    // statement names tables the user may not have.
    runs: "skip",
  });
  add({
    id: "execution/initial",
    label: "Execution workspace opener",
    sql: EXECUTION_INITIAL_SQL,
    // It creates its own table, so it needs a database that has not got one.
    on: "empty",
  });
  add({
    id: "execution/joins",
    label: "Joins and aggregation",
    sql: JOIN_DEMO_SQL,
    on: "empty",
  });
  add({
    id: "execution/outer-joins",
    label: "Outer joins",
    sql: OUTER_JOIN_DEMO_SQL,
    on: "empty",
  });
  add({
    id: "sample/seed",
    label: "Sample database",
    sql: sampleDatabaseSql(),
    on: "empty",
  });
  add({
    id: "execution/outer-join-simplification",
    label: "When an outer join is not one",
    sql: SIMPLIFIED_OUTER_JOIN_DEMO_SQL,
    on: "empty",
  });
  return out;
}
