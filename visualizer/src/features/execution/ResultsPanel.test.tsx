/**
 * The results footer, and one badge that lied for thirteen milestones.
 *
 * `seq scan` was a literal string from Milestone 3 until Milestone 16. Index
 * scans arrived in Milestone 5 and joins in Milestone 13, so from that point on
 * the badge was wrong for every query the planner did anything interesting
 * with, and it was wrong in the most persuasive way available, by naming a
 * real access path with total confidence.
 *
 * Nothing caught it because no test rendered this footer. These do.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResultsPanel } from "./ResultsPanel";
import type {
  OperatorNodeModel,
  PlanModel,
  QueryResultModel,
} from "@/types/api";

function node(operator_type: string, children: string[] = []): OperatorNodeModel {
  return {
    operator_id: `${operator_type.toLowerCase()}_1`,
    operator_type,
    detail: "",
    children,
    output_columns: [{ name: "email", type: "TEXT" }],
    estimated_rows: null,
    estimated_cost: null,
    estimated_io_cost: null,
    estimated_cpu_cost: null,
  } as OperatorNodeModel;
}

function plan(...types: string[]): PlanModel {
  return {
    nodes: types.map((type) => node(type)),
    root_id: `${types[0]?.toLowerCase()}_1`,
    alternatives: [],
    rewrites: [],
    estimated_cost: 12.5,
    statistics: null,
  } as PlanModel;
}

function result(overrides: Partial<QueryResultModel> = {}): QueryResultModel {
  return {
    statement_kind: "SelectStatement",
    returns_rows: true,
    message: "",
    columns: [{ name: "email", type: "TEXT" }],
    rows: [["ada@example.com"]],
    record_ids: [],
    plan: null,
    rows_returned: 1,
    rows_affected: 0,
    rows_scanned: 4,
    rows_rejected: 3,
    pages_read: 2,
    pages_written: 0,
    duration_ns: 91_000,
    truncated: false,
    cancelled: false,
    ...overrides,
  } as QueryResultModel;
}

/** The panel shows the *last* statement of a script, so one is enough. */
const render1 = (r: QueryResultModel) =>
  render(<ResultsPanel results={[r]} isPending={false} error={undefined} />);

describe("the access path badge", () => {
  it("says index scan when the planner chose one", () => {
    render1(result({ plan: plan("Project", "IndexScan") }));
    expect(screen.getByText("index scan")).toBeInTheDocument();
    expect(screen.queryByText("seq scan")).not.toBeInTheDocument();
  });

  it("says seq scan when the planner chose that", () => {
    render1(result({ plan: plan("Project", "Filter", "SeqScan") }));
    expect(screen.getByText("seq scan")).toBeInTheDocument();
  });

  it("reports both sides of a join that reads them differently", () => {
    // The interesting case, and the one a single hardcoded label could never
    // express: a hash join with an index on the build side only.
    render1(result({ plan: plan("HashJoin", "IndexScan", "SeqScan") }));
    expect(screen.getByText("index scan + seq scan")).toBeInTheDocument();
  });

  // `queryByText(/scan/)` would match the "scanned 4" counter, so the badge is
  // asked for by its exact labels.
  const noBadge = () => {
    for (const label of ["seq scan", "index scan", "index scan + seq scan"]) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  };

  it("says nothing at all when there is no plan", () => {
    // An INSERT has no operator tree. Naming an access path for it would be
    // inventing one.
    render1(result({ plan: null, statement_kind: "InsertStatement" }));
    noBadge();
  });

  it("says nothing when a plan somehow has no scan in it", () => {
    render1(result({ plan: plan("Project") }));
    noBadge();
  });
});

describe("the footer counters", () => {
  it("reports what the statement cost", () => {
    render1(result({ plan: plan("SeqScan") }));
    expect(screen.getByText(/scanned 4/)).toBeInTheDocument();
    expect(screen.getByText(/returned 1/)).toBeInTheDocument();
  });

  it("does not claim the buffer pool is absent", () => {
    // It shipped in Milestone 7; the tooltip said otherwise until Milestone 16.
    const { container } = render1(result({ plan: plan("SeqScan") }));
    expect(container.innerHTML).not.toMatch(/no buffer pool/i);
    expect(container.innerHTML).not.toMatch(/until Milestone/i);
  });
});
