/**
 * The SQL workspace: editor on the left, pipeline on the right.
 *
 * The two are linked in both directions:
 *
 *   click an AST node / token  →  its source range highlights in the editor
 *   move the editor cursor     →  the innermost node containing it selects
 *
 * "Innermost" is the useful choice: putting the cursor inside `18` in
 * `age >= 18` should select the `Literal`, not the whole `SelectStatement` that
 * also contains it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SplitPane } from "@/components/SplitPane";
import { useParseSql } from "@/hooks/useEngine";
import { SQL_INITIAL_SQL } from "@/lib/demoSql";
import type { AstNodeModel, ParseResponse } from "@/types/api";
import { PipelinePanel, type Selection } from "./PipelinePanel";
import { SqlEditor } from "./SqlEditor";

const STORAGE_KEY = "chendb.sql";

/** How long to wait after a keystroke before re-parsing. */
const DEBOUNCE_MS = 250;

/**
 * The smallest node whose span contains `offset`.
 *
 * Ties are broken by span length, so a leaf wins over its ancestors. Nodes with
 * an empty span are skipped — they cannot meaningfully contain a cursor.
 */
function innermostNodeAt(
  nodes: AstNodeModel[],
  offset: number,
): AstNodeModel | null {
  let best: AstNodeModel | null = null;
  for (const node of nodes) {
    if (node.start > offset || offset > node.end) continue;
    if (node.end === node.start) continue;
    if (best === null || node.end - node.start < best.end - best.start) best = node;
  }
  return best;
}

export function SqlWorkspace({
  databaseId,
  theme,
}: {
  databaseId: string;
  theme: "light" | "dark";
}) {
  const [sql, setSql] = useState<string>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) ?? SQL_INITIAL_SQL;
    } catch {
      return SQL_INITIAL_SQL;
    }
  });
  const [selection, setSelection] = useState<Selection>(null);
  const [view, setView] = useState<"tokens" | "ast">("ast");
  const [result, setResult] = useState<ParseResponse | undefined>();

  const parse = useParseSql(databaseId);
  const debounceRef = useRef<number | undefined>(undefined);

  const runParse = useCallback(
    (text: string) => {
      parse.mutate(text, { onSuccess: setResult });
    },
    [parse],
  );

  // Parse as the user types, debounced. Immediate parsing on every keystroke
  // would be a request per character; waiting for an explicit action would make
  // the panels feel stale.
  useEffect(() => {
    window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => runParse(sql), DEBOUNCE_MS);
    return () => window.clearTimeout(debounceRef.current);
    // runParse changes identity on every mutation state change; depending on it
    // would restart the timer constantly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sql, databaseId]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, sql);
    } catch {
      // Storage may be unavailable; the editor still works this session.
    }
  }, [sql]);

  // A stale selection would highlight bytes that no longer belong to it.
  useEffect(() => setSelection(null), [result?.sql]);

  const highlight = useMemo(() => {
    if (!result || !selection) return null;
    if (selection.kind === "token") {
      const token = result.tokens[selection.index];
      return token ? { start: token.start, end: token.end } : null;
    }
    const node = result.ast.nodes.find((n) => n.node_id === selection.nodeId);
    return node ? { start: node.start, end: node.end } : null;
  }, [result, selection]);

  const onCursorOffset = useCallback(
    (offset: number) => {
      if (!result || view !== "ast") return;
      const node = innermostNodeAt(result.ast.nodes, offset);
      setSelection(node ? { kind: "node", nodeId: node.node_id } : null);
    },
    [result, view],
  );

  return (
    <SplitPane
      direction="horizontal"
      initialPercent={50}
      minPercent={28}
      maxPercent={72}
      label="Resize the editor against the pipeline"
      className="min-h-0 w-full"
      first={
        <div className="min-h-0 w-full pr-1">
          <SqlEditor
            sql={sql}
            onChange={setSql}
            onParse={() => runParse(sql)}
            result={result}
            isPending={parse.isPending}
            theme={theme}
            highlight={highlight}
            onCursorOffset={onCursorOffset}
          />
        </div>
      }
      second={
        <div className="min-h-0 w-full pl-1">
          <PipelinePanel
            result={result}
            selection={selection}
            onSelect={setSelection}
            view={view}
            onChangeView={setView}
          />
        </div>
      }
    />
  );
}
