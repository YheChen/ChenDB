/**
 * Monaco setup: local, offline, and configured once.
 *
 * `@monaco-editor/react` loads Monaco from a CDN by default. That breaks
 * offline use and adds a third-party runtime dependency to a local development
 * tool, so the bundled copy is wired in explicitly instead.
 *
 * Monaco also needs a web worker. Only the base editor worker is required —
 * there is no TypeScript or JSON language service here, just SQL tokenizing —
 * so exactly one worker is registered.
 *
 * The import is `editor.api` rather than the `monaco-editor` entry point: that
 * entry re-exports every built-in language (TypeScript, JSON, CSS, HTML, ...)
 * and their workers, roughly tripling the bundle. ChenDB registers its own SQL
 * grammar below, so none of them are wanted.
 */

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";

declare global {
  interface Window {
    MonacoEnvironment?: monaco.Environment;
  }
}

let configured = false;

/** Idempotent. Safe to call from every component that mounts an editor. */
export function configureMonaco(): void {
  if (configured) return;
  configured = true;

  self.MonacoEnvironment = {
    getWorker: () => new editorWorker(),
  };
  loader.config({ monaco });

  // ChenDB's dialect is narrower than Monaco's built-in `sql` language, so the
  // keyword list is replaced: highlighting a word the parser will reject as a
  // keyword would be actively misleading.
  monaco.languages.register({ id: CHENDB_SQL });
  monaco.languages.setMonarchTokensProvider(CHENDB_SQL, MONARCH);
  monaco.languages.setLanguageConfiguration(CHENDB_SQL, {
    comments: { lineComment: "--", blockComment: ["/*", "*/"] },
    brackets: [["(", ")"]],
    autoClosingPairs: [
      { open: "(", close: ")" },
      { open: "'", close: "'" },
      { open: '"', close: '"' },
    ],
  });

  monaco.editor.defineTheme("chendb-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [],
    colors: { "editor.background": "#00000000" },
  });
  monaco.editor.defineTheme("chendb-light", {
    base: "vs",
    inherit: true,
    rules: [],
    colors: { "editor.background": "#00000000" },
  });
}

export const CHENDB_SQL = "chendb-sql";

/** Keywords the engine's lexer actually recognises. Mirrors `Keyword` in Python. */
const KEYWORDS = [
  "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "EXPLAIN",
  "FROM", "WHERE", "INTO", "VALUES", "SET", "TABLE", "INDEX", "ON", "AS",
  "ORDER", "GROUP", "BY", "LIMIT", "OFFSET", "ASC", "DESC", "DISTINCT",
  "AND", "OR", "NOT", "IS", "IN", "LIKE", "BETWEEN",
  "NULL", "TRUE", "FALSE",
  "PRIMARY", "KEY", "UNIQUE", "DEFAULT", "IF", "EXISTS",
  "BEGIN", "COMMIT", "ROLLBACK",
];

const TYPE_KEYWORDS = [
  "INTEGER", "INT", "BIGINT", "FLOAT", "REAL", "DOUBLE",
  "BOOLEAN", "BOOL", "TEXT", "VARCHAR",
];

const MONARCH: monaco.languages.IMonarchLanguage = {
  ignoreCase: true,
  keywords: KEYWORDS,
  typeKeywords: TYPE_KEYWORDS,
  operators: ["=", "<>", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "%"],
  tokenizer: {
    root: [
      [/--.*$/, "comment"],
      [/\/\*/, "comment", "@comment"],
      [
        /[a-zA-Z_]\w*/,
        {
          cases: {
            "@keywords": "keyword",
            "@typeKeywords": "type",
            "@default": "identifier",
          },
        },
      ],
      [/"([^"\\]|"")*"/, "identifier"],
      [/\d+\.\d+([eE][+-]?\d+)?/, "number.float"],
      [/\d+[eE][+-]?\d+/, "number.float"],
      [/\d+/, "number"],
      [/'([^']|'')*'/, "string"],
      [/'/, "string.invalid"],
      [/<=|>=|<>|!=|[=<>+\-*/%]/, "operator"],
      [/[(),;.]/, "delimiter"],
      [/\s+/, "white"],
    ],
    comment: [
      [/[^/*]+/, "comment"],
      [/\*\//, "comment", "@pop"],
      [/[/*]/, "comment"],
    ],
  },
};
