/**
 * The SQL transformation pipeline: tokens, then the AST.
 *
 *     SQL source  →  Tokens  →  Abstract syntax tree  →  (Bound statement, M4)
 *
 * Both views are selectable, and selecting anything highlights the source range
 * it came from. The AST tree is rendered from the flat node list the API
 * returns, reassembled by `children` ids, random access by node_id is what
 * makes "find the innermost node containing the cursor" cheap.
 */

import { useMemo } from "react";
import { Badge, EmptyState, Panel } from "@/components/primitives";
import { cn } from "@/lib/format";
import type { AstNodeModel, ParseResponse, TokenModel } from "@/types/api";

export type Selection =
  | { kind: "token"; index: number }
  | { kind: "node"; nodeId: number }
  | null;

const TOKEN_TONE: Record<string, string> = {
  keyword: "text-violet-600 dark:text-violet-300",
  identifier: "text-sky-700 dark:text-sky-300",
  int_literal: "text-emerald-700 dark:text-emerald-300",
  float_literal: "text-emerald-700 dark:text-emerald-300",
  string_literal: "text-amber-700 dark:text-amber-400",
  eof: "text-[var(--text-secondary)] opacity-50",
};

function tokenTone(type: string): string {
  return TOKEN_TONE[type] ?? "text-[var(--text-secondary)]";
}

/** Node types that are structure rather than data, shown less prominently. */
const STRUCTURAL = new Set(["SelectItem", "ValuesRow"]);

export function PipelinePanel({
  result,
  selection,
  onSelect,
  view,
  onChangeView,
}: {
  result: ParseResponse | undefined;
  selection: Selection;
  onSelect: (next: Selection) => void;
  view: "tokens" | "ast";
  onChangeView: (next: "tokens" | "ast") => void;
}) {
  const nodesById = useMemo(() => {
    const map = new Map<number, AstNodeModel>();
    for (const node of result?.ast.nodes ?? []) map.set(node.node_id, node);
    return map;
  }, [result]);

  const subtitle = result
    ? view === "tokens"
      ? `${result.token_count} tokens`
      : `${result.node_count} nodes · ${result.statements.length} statement(s)`
    : undefined;

  return (
    <Panel
      title="Pipeline"
      subtitle={subtitle}
      className="h-full"
      bodyClassName="flex flex-col"
      actions={
        <div role="tablist" aria-label="Pipeline stage" className="flex gap-1">
          {(["tokens", "ast"] as const).map((stage) => (
            <button
              key={stage}
              role="tab"
              type="button"
              aria-selected={view === stage}
              onClick={() => onChangeView(stage)}
              className={cn(
                "rounded px-2 py-0.5 text-[11px] font-medium transition-colors",
                view === stage
                  ? "bg-[var(--accent)] text-white"
                  : "hover:bg-[var(--surface-sunken)]",
              )}
            >
              {stage === "tokens" ? "Tokens" : "AST"}
            </button>
          ))}
        </div>
      }
    >
      {!result ? (
        <EmptyState
          title="Nothing parsed yet"
          hint="Write some SQL and press ⌘↵. Tokens appear even when the statement is incomplete."
        />
      ) : view === "tokens" ? (
        <TokenList
          tokens={result.tokens}
          lexedOk={result.lexed_ok}
          selection={selection}
          onSelect={onSelect}
        />
      ) : (
        <AstTree
          result={result}
          nodesById={nodesById}
          selection={selection}
          onSelect={onSelect}
        />
      )}
    </Panel>
  );
}

function TokenList({
  tokens,
  lexedOk,
  selection,
  onSelect,
}: {
  tokens: TokenModel[];
  lexedOk: boolean;
  selection: Selection;
  onSelect: (next: Selection) => void;
}) {
  if (tokens.length === 0) {
    return (
      <EmptyState
        title={lexedOk ? "No tokens" : "Tokenizing failed"}
        hint={
          lexedOk
            ? "The input is empty or contains only comments."
            : "The scanner could not get past a character. See the error under the editor."
        }
      />
    );
  }

  return (
    <ul className="divide-y divide-[var(--border-subtle)] font-mono text-[11px]">
      {tokens.map((token) => {
        const selected = selection?.kind === "token" && selection.index === token.index;
        return (
          <li key={token.index}>
            <button
              type="button"
              onClick={() => onSelect(selected ? null : { kind: "token", index: token.index })}
              aria-pressed={selected}
              className={cn(
                "flex w-full items-baseline gap-2 px-3 py-1 text-left",
                selected ? "bg-[var(--accent)]/12" : "hover:bg-[var(--surface-sunken)]",
              )}
            >
              <span className="text-muted w-7 shrink-0 text-right">{token.index}</span>
              <span className={cn("w-28 shrink-0 truncate", tokenTone(token.type))}>
                {token.type}
              </span>
              <span className="min-w-0 flex-1 truncate font-semibold">
                {token.lexeme || "∅"}
              </span>
              {token.keyword ? <Badge tone="meta">{token.keyword}</Badge> : null}
              {token.value !== null && token.value !== undefined ? (
                <Badge tone="heap" title="Decoded literal value">
                  {JSON.stringify(token.value)}
                </Badge>
              ) : null}
              <span className="text-muted w-20 shrink-0 text-right">
                [{token.start}:{token.end}]
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function AstTree({
  result,
  nodesById,
  selection,
  onSelect,
}: {
  result: ParseResponse;
  nodesById: Map<number, AstNodeModel>;
  selection: Selection;
  onSelect: (next: Selection) => void;
}) {
  if (result.ast.root_ids.length === 0) {
    return (
      <EmptyState
        title="No statements"
        hint={
          result.error
            ? "Parsing stopped before a statement completed. The token view still shows what scanned."
            : "The input is empty or contains only comments."
        }
      />
    );
  }

  return (
    <div className="py-1">
      {result.ast.root_ids.map((rootId, index) => (
        // Keyed by position, not by root id: position is what identifies a
        // statement within the script.
        <div key={`${index}-${rootId}`}>
          {result.ast.root_ids.length > 1 ? (
            <p className="text-muted px-3 py-1 text-[10px] tracking-wide uppercase">
              statement {index + 1}
            </p>
          ) : null}
          <AstRow
            nodeId={rootId}
            nodesById={nodesById}
            depth={0}
            selection={selection}
            onSelect={onSelect}
          />
        </div>
      ))}
    </div>
  );
}

function AstRow({
  nodeId,
  nodesById,
  depth,
  selection,
  onSelect,
}: {
  nodeId: number;
  nodesById: Map<number, AstNodeModel>;
  depth: number;
  selection: Selection;
  onSelect: (next: Selection) => void;
}) {
  const node = nodesById.get(nodeId);
  if (!node) return null;

  const selected = selection?.kind === "node" && selection.nodeId === nodeId;
  const attributes = Object.entries(node.attributes).filter(
    ([, value]) =>
      value !== null &&
      value !== undefined &&
      value !== false &&
      !(Array.isArray(value) && value.length === 0),
  );

  return (
    <>
      <button
        type="button"
        onClick={() => onSelect(selected ? null : { kind: "node", nodeId })}
        aria-pressed={selected}
        title={`${node.node_type} · characters ${node.start}–${node.end} · line ${node.line}, column ${node.column}`}
        className={cn(
          "flex w-full items-baseline gap-2 px-3 py-0.5 text-left font-mono text-[11px]",
          selected ? "bg-[var(--accent)]/12" : "hover:bg-[var(--surface-sunken)]",
        )}
      >
        <span
          aria-hidden
          className="text-muted shrink-0 whitespace-pre select-none opacity-50"
        >
          {depth === 0 ? "" : `${"│  ".repeat(depth - 1)}└─ `}
        </span>
        <span
          className={cn(
            "shrink-0 font-sans font-medium",
            STRUCTURAL.has(node.node_type) ? "text-muted" : "",
          )}
        >
          {node.node_type}
        </span>
        {node.label ? (
          <span className="shrink-0 font-semibold text-[var(--accent)]">
            {node.label}
          </span>
        ) : null}
        {attributes
          .filter(([key]) => key !== "value" && key !== "name" && key !== "operator")
          .map(([key, value]) => (
            <span key={key} className="text-muted shrink-0 text-[10px]">
              {key}={Array.isArray(value) ? value.join(",") : String(value)}
            </span>
          ))}
        <span className="text-muted min-w-0 flex-1 truncate text-right text-[10px] opacity-70">
          {node.text}
        </span>
      </button>
      {node.children.map((childId) => (
        <AstRow
          key={childId}
          nodeId={childId}
          nodesById={nodesById}
          depth={depth + 1}
          selection={selection}
          onSelect={onSelect}
        />
      ))}
    </>
  );
}
