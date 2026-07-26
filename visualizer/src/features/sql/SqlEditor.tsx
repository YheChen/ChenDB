/**
 * The SQL editor.
 *
 * Two-way highlighting is the point of this panel:
 *
 *   selecting an AST node or token  →  its source range highlights here
 *   moving the cursor here          →  the innermost node containing it selects
 *
 * That link is what makes an AST legible. Every node carries the exact
 * character range it was parsed from, so the mapping is exact rather than
 * approximate.
 */

import Editor, { type OnMount } from "@monaco-editor/react";
import { useCallback, useEffect, useRef } from "react";
import type * as monacoNs from "monaco-editor/esm/vs/editor/editor.api";
import { Button, Panel } from "@/components/primitives";
import { CHENDB_SQL, configureMonaco } from "@/lib/monaco";
import { formatDuration } from "@/lib/format";
import type { ParseResponse, SqlErrorModel } from "@/types/api";

/** `expected` is optional on the wire; normalise it once. */
function expectedList(error: SqlErrorModel): string[] {
  return error.expected ?? [];
}

configureMonaco();

const EXAMPLES: { label: string; sql: string }[] = [
  {
    label: "CREATE TABLE",
    sql: `CREATE TABLE users (
  id     INTEGER PRIMARY KEY,
  email  TEXT NOT NULL,
  age    INTEGER,
  active BOOLEAN
);`,
  },
  {
    label: "INSERT",
    sql: `INSERT INTO users (id, email, age, active) VALUES
  (1, 'ada@example.com', 36, TRUE),
  (2, 'alan@example.com', NULL, FALSE);`,
  },
  {
    label: "SELECT with WHERE",
    sql: `SELECT email, age * 2 AS doubled
FROM users
WHERE age >= 18 AND email IS NOT NULL;`,
  },
  {
    label: "Operator precedence",
    sql: `-- Parses as: a = 1 OR (b = 2 AND c = 3)
SELECT * FROM t WHERE a = 1 OR b = 2 AND c = 3;`,
  },
  {
    label: "Quoted identifier",
    sql: `-- "select" is reserved; quoting makes it a name
SELECT "select" FROM "order";`,
  },
  {
    label: "A syntax error",
    sql: `SELECT name FROM`,
  },
  {
    label: "Not implemented yet",
    sql: `SELECT * FROM users ORDER BY age;`,
  },
];

export function SqlEditor({
  sql,
  onChange,
  onParse,
  result,
  isPending,
  theme,
  highlight,
  onCursorOffset,
  runLabel = "Parse ⌘↵",
  title = "SQL",
}: {
  sql: string;
  onChange: (next: string) => void;
  onParse: () => void;
  result: ParseResponse | undefined;
  isPending: boolean;
  theme: "light" | "dark";
  /** Character range to highlight, from the AST or token selection. */
  highlight: { start: number; end: number } | null;
  onCursorOffset: (offset: number) => void;
  /** The primary action's label. "Parse ⌘↵" in the SQL workspace, "Run ⌘↵" in Execution. */
  runLabel?: string;
  title?: string;
}) {
  const editorRef = useRef<monacoNs.editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<typeof monacoNs | null>(null);
  const decorationsRef = useRef<monacoNs.editor.IEditorDecorationsCollection | null>(
    null,
  );

  const onMount: OnMount = useCallback(
    (editor, monaco) => {
      editorRef.current = editor;
      monacoRef.current = monaco;
      decorationsRef.current = editor.createDecorationsCollection([]);

      editor.addCommand(
        monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter,
        () => onParse(),
      );
      editor.onDidChangeCursorPosition((event) => {
        const offset = editor.getModel()?.getOffsetAt(event.position);
        if (offset !== undefined) onCursorOffset(offset);
      });
    },
    [onParse, onCursorOffset],
  );

  // Error markers. Monaco owns the squiggle; the engine owns the position.
  useEffect(() => {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const model = editor?.getModel();
    if (!editor || !monaco || !model) return;

    if (!result?.error) {
      monaco.editor.setModelMarkers(model, "chendb", []);
      return;
    }
    const error = result.error;
    const from = model.getPositionAt(error.start);
    const to = model.getPositionAt(Math.max(error.end, error.start + 1));
    monaco.editor.setModelMarkers(model, "chendb", [
      {
        severity:
          error.kind === "UnsupportedSqlError"
            ? monaco.MarkerSeverity.Warning
            : monaco.MarkerSeverity.Error,
        message: expectedList(error).length
          ? `${error.message}\n\nExpected: ${expectedList(error).join(", ")}`
          : error.message,
        startLineNumber: from.lineNumber,
        startColumn: from.column,
        endLineNumber: to.lineNumber,
        endColumn: to.column,
        source: error.kind,
      },
    ]);
  }, [result]);

  // Selection highlight, driven by whatever node or token is selected.
  useEffect(() => {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const model = editor?.getModel();
    const collection = decorationsRef.current;
    if (!editor || !monaco || !model || !collection) return;

    if (!highlight || highlight.end <= highlight.start) {
      collection.set([]);
      return;
    }
    const from = model.getPositionAt(highlight.start);
    const to = model.getPositionAt(highlight.end);
    collection.set([
      {
        range: new monaco.Range(
          from.lineNumber,
          from.column,
          to.lineNumber,
          to.column,
        ),
        options: {
          className: "chendb-span-highlight",
          stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges,
        },
      },
    ]);
    editor.revealRangeInCenterIfOutsideViewport(
      new monaco.Range(from.lineNumber, from.column, to.lineNumber, to.column),
    );
  }, [highlight]);

  const status = () => {
    if (isPending) return "parsing…";
    if (!result) return "⌘↵ to parse";
    if (result.error) {
      const kind = result.error.kind === "UnsupportedSqlError" ? "unsupported" : "error";
      return `${kind} at line ${result.error.line}, column ${result.error.column}`;
    }
    const plural = result.statements.length === 1 ? "statement" : "statements";
    return `${result.statements.length} ${plural} · ${result.token_count} tokens · ${result.node_count} nodes · ${formatDuration(result.duration_ns)}`;
  };

  return (
    <Panel
      title={title}
      subtitle={status()}
      className="h-full"
      bodyClassName="flex flex-col"
      actions={
        <>
          <select
            aria-label="Load an example"
            value=""
            onChange={(event) => {
              const example = EXAMPLES.find((e) => e.label === event.target.value);
              if (example) onChange(example.sql);
            }}
            className="surface-sunken max-w-40 rounded border border-[var(--border-subtle)] px-1.5 py-0.5 text-[11px]"
          >
            <option value="">Examples…</option>
            {EXAMPLES.map((example) => (
              <option key={example.label} value={example.label}>
                {example.label}
              </option>
            ))}
          </select>
          <Button variant="primary" onClick={onParse} disabled={isPending}>
            {runLabel}
          </Button>
        </>
      }
    >
      <div className="min-h-0 flex-1">
        <Editor
          language={CHENDB_SQL}
          theme={theme === "dark" ? "chendb-dark" : "chendb-light"}
          value={sql}
          onChange={(next) => onChange(next ?? "")}
          onMount={onMount}
          options={{
            fontSize: 13,
            fontFamily:
              'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace',
            minimap: { enabled: false },
            lineNumbers: "on",
            scrollBeyondLastLine: false,
            renderLineHighlight: "line",
            automaticLayout: true,
            tabSize: 2,
            padding: { top: 8, bottom: 8 },
            // No IntelliSense: there is no binder yet, so any suggestion would
            // be a guess. Milestone 4's catalog makes real completion possible.
            quickSuggestions: false,
            suggestOnTriggerCharacters: false,
            wordBasedSuggestions: "off",
            occurrencesHighlight: "off",
            overviewRulerLanes: 0,
            scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
          }}
        />
      </div>

      {result?.error ? (
        <div
          role="alert"
          className={`shrink-0 border-t px-3 py-1.5 font-mono text-[11px] ${
            result.error.kind === "UnsupportedSqlError"
              ? "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300"
              : "border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-300"
          }`}
        >
          <span className="font-sans font-semibold">{result.error.kind}</span>{" "}
          {result.error.message}
          {expectedList(result.error).length > 0 ? (
            <span className="opacity-70">
              {" "}
              · expected {expectedList(result.error).join(" | ")}
            </span>
          ) : null}
        </div>
      ) : null}
    </Panel>
  );
}
