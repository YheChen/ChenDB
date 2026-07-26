import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PipelinePanel } from "./PipelinePanel";
import type { AstNodeModel, ParseResponse, TokenModel } from "@/types/api";

const SQL = "SELECT age FROM t WHERE age >= 18";

function token(
  index: number,
  type: string,
  lexeme: string,
  start: number,
  extra: Partial<TokenModel> = {},
): TokenModel {
  return {
    index,
    type,
    lexeme,
    start,
    end: start + lexeme.length,
    line: 1,
    column: start + 1,
    keyword: null,
    value: null,
    ...extra,
  };
}

function node(
  node_id: number,
  node_type: string,
  start: number,
  end: number,
  extra: Partial<AstNodeModel> = {},
): AstNodeModel {
  return {
    node_id,
    node_type,
    start,
    end,
    line: 1,
    column: start + 1,
    text: SQL.slice(start, end),
    children: [],
    attributes: {},
    label: "",
    ...extra,
  };
}

const RESULT: ParseResponse = {
  sql: SQL,
  ok: true,
  lexed_ok: true,
  token_count: 3,
  node_count: 4,
  duration_ns: 120_000,
  tokens: [
    token(0, "keyword", "SELECT", 0, { keyword: "SELECT" }),
    token(1, "identifier", "age", 7),
    token(2, "int_literal", "18", 30, { value: 18 }),
  ],
  statements: [
    { root_id: 3, kind: "SelectStatement", start: 0, end: SQL.length, text: SQL },
  ],
  ast: {
    root_ids: [3],
    nodes: [
      node(0, "ColumnRef", 24, 27, { label: "age", attributes: { name: "age" } }),
      node(1, "Literal", 30, 32, {
        label: "18",
        attributes: { value: 18, data_type: "INTEGER" },
      }),
      node(2, "BinaryOp", 24, 32, {
        label: ">=",
        children: [0, 1],
        attributes: { operator: ">=" },
      }),
      node(3, "SelectStatement", 0, SQL.length, { children: [2] }),
    ],
  },
};

function renderPanel(props: Partial<Parameters<typeof PipelinePanel>[0]> = {}) {
  return render(
    <PipelinePanel
      result={RESULT}
      selection={null}
      onSelect={vi.fn()}
      view="ast"
      onChangeView={vi.fn()}
      {...props}
    />,
  );
}

describe("PipelinePanel", () => {
  it("prompts before anything has been parsed", () => {
    renderPanel({ result: undefined });
    expect(screen.getByText("Nothing parsed yet")).toBeInTheDocument();
  });

  it("renders the AST tree from the flat node list", () => {
    renderPanel();
    // The tree is reassembled from `children` ids, so every node must appear
    // exactly once even though the API sends a flat array.
    expect(screen.getByText("SelectStatement")).toBeInTheDocument();
    expect(screen.getByText("BinaryOp")).toBeInTheDocument();
    expect(screen.getAllByText("ColumnRef")).toHaveLength(1);
    expect(screen.getByText(">=")).toBeInTheDocument();
  });

  it("exposes each node's span and position in its tooltip", () => {
    renderPanel();
    expect(
      screen.getByTitle("BinaryOp · characters 24–32 · line 1, column 25"),
    ).toBeInTheDocument();
  });

  it("selects a node on click and deselects on a second click", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    const { rerender } = renderPanel({ onSelect });

    await user.click(screen.getByTitle(/^BinaryOp/));
    expect(onSelect).toHaveBeenCalledWith({ kind: "node", nodeId: 2 });

    rerender(
      <PipelinePanel
        result={RESULT}
        selection={{ kind: "node", nodeId: 2 }}
        onSelect={onSelect}
        view="ast"
        onChangeView={vi.fn()}
      />,
    );
    await user.click(screen.getByTitle(/^BinaryOp/));
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("marks the selected node for assistive technology", () => {
    renderPanel({ selection: { kind: "node", nodeId: 2 } });
    expect(screen.getByTitle(/^BinaryOp/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTitle(/^ColumnRef/)).toHaveAttribute("aria-pressed", "false");
  });

  it("switches to the token view", async () => {
    const onChangeView = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onChangeView });

    await user.click(screen.getByRole("tab", { name: "Tokens" }));
    expect(onChangeView).toHaveBeenCalledWith("tokens");
  });

  it("lists tokens with their type, keyword and decoded value", () => {
    renderPanel({ view: "tokens" });
    // "SELECT" appears twice: as the lexeme and as the keyword badge.
    expect(screen.getAllByText("SELECT")).toHaveLength(2);
    expect(screen.getByText("keyword")).toBeInTheDocument();
    expect(screen.getByText("int_literal")).toBeInTheDocument();
    expect(screen.getByText("identifier")).toBeInTheDocument();
    // Decoded literal values are shown, not just the source text.
    expect(screen.getAllByText("18").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("[30:32]")).toBeInTheDocument();
  });

  it("selects a token on click", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    renderPanel({ view: "tokens", onSelect });

    await user.click(screen.getByText("identifier").closest("button")!);
    expect(onSelect).toHaveBeenCalledWith({ kind: "token", index: 1 });
  });

  it("explains an empty AST when parsing failed part-way", () => {
    renderPanel({
      result: {
        ...RESULT,
        ok: false,
        statements: [],
        ast: { nodes: [], root_ids: [] },
        error: {
          kind: "ParseError",
          message: "expected a table name, found end of input",
          start: 16,
          end: 17,
          line: 1,
          column: 17,
          expected: ["a table name"],
          found: "end of input",
        },
      },
    });
    expect(screen.getByText("No statements")).toBeInTheDocument();
    expect(screen.getByText(/token view still shows what scanned/)).toBeInTheDocument();
  });

  it("distinguishes a failed tokenize from an empty input", () => {
    renderPanel({
      view: "tokens",
      result: { ...RESULT, ok: false, lexed_ok: false, tokens: [], token_count: 0 },
    });
    expect(screen.getByText("Tokenizing failed")).toBeInTheDocument();
  });

  it("labels each statement when a script has several", () => {
    renderPanel({
      result: {
        ...RESULT,
        ast: { ...RESULT.ast, root_ids: [3, 3] },
        statements: [RESULT.statements[0]!, RESULT.statements[0]!],
      },
    });
    expect(screen.getByText("statement 1")).toBeInTheDocument();
    expect(screen.getByText("statement 2")).toBeInTheDocument();
  });
});
