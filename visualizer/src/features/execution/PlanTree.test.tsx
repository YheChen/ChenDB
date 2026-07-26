import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AlternativesPanel, PlanTree } from "./PlanTree";
import type { OperatorNodeModel, PlanModel } from "@/types/api";

function node(
  operator_id: string,
  operator_type: string,
  overrides: Partial<OperatorNodeModel> = {},
): OperatorNodeModel {
  return {
    operator_id,
    operator_type,
    detail: "",
    children: [],
    output_columns: [{ name: "email", type: "TEXT" }],
    estimated_rows: null,
    estimated_cost: null,
    estimated_io_cost: null,
    estimated_cpu_cost: null,
    next_calls: 0,
    input_rows: 0,
    output_rows: 0,
    rows_rejected: 0,
    duration_ns: 1000,
    ...overrides,
  };
}

/** A plan with no planner metadata — the shape a pre-Milestone-6 plan had. */
function plan(root_id: string, nodes: OperatorNodeModel[]): PlanModel {
  return {
    root_id,
    nodes,
    alternatives: [],
    rewrites: [],
    estimated_cost: null,
    statistics: null,
  };
}

/** Project ← Filter ← SeqScan, with the statistics a real run produces. */
const PLAN: PlanModel = plan("project_1", [
    node("project_1", "Project", {
      detail: "email, (age * 2)",
      children: ["filter_1"],
      input_rows: 2,
      output_rows: 2,
      next_calls: 3,
    }),
    node("filter_1", "Filter", {
      detail: "(age >= 18)",
      children: ["scan_1"],
      input_rows: 4,
      output_rows: 2,
      rows_rejected: 2,
      next_calls: 3,
    }),
    node("scan_1", "SeqScan", {
      detail: "table=users",
      output_rows: 4,
      next_calls: 5,
    }),
]);

describe("PlanTree", () => {
  it("explains why a statement has no plan", () => {
    render(<PlanTree plan={null} />);
    expect(screen.getByText("No plan")).toBeInTheDocument();
    expect(screen.getByText(/INSERT and CREATE TABLE have no operator tree/)).toBeInTheDocument();
  });

  it("renders the tree root-first from the flat node list", () => {
    render(<PlanTree plan={PLAN} />);
    for (const type of ["Project", "Filter", "SeqScan"]) {
      expect(screen.getByText(type)).toBeInTheDocument();
    }
    expect(screen.getByText("(age >= 18)")).toBeInTheDocument();
    expect(screen.getByText("table=users")).toBeInTheDocument();
  });

  it("labels the direction of data flow, which is the confusing part", () => {
    render(<PlanTree plan={PLAN} />);
    expect(
      screen.getByText("rows flow up · next() calls travel down"),
    ).toBeInTheDocument();
  });

  it("shows each operator's actual row counts", () => {
    render(<PlanTree plan={PLAN} />);
    // The filter consumed 4 and produced 2.
    expect(
      screen.getByTitle("4 rows in, 2 out, over 3 next() calls"),
    ).toBeInTheDocument();
  });

  it("flags rejected rows only on operators that reject", () => {
    render(<PlanTree plan={PLAN} />);
    const rejected = screen.getByText("−2");
    expect(rejected).toBeInTheDocument();
    expect(rejected).toHaveAttribute(
      "title",
      expect.stringContaining("NULL, which is not TRUE"),
    );
    // A scan rejects nothing, so no badge for it.
    expect(screen.queryByText("−0")).not.toBeInTheDocument();
  });

  it("names every operator for assistive technology", () => {
    render(<PlanTree plan={PLAN} />);
    expect(
      screen.getByRole("button", { name: "SeqScan scan_1, 4 rows out" }),
    ).toBeInTheDocument();
  });

  it("marks the operator the engine is paused inside", () => {
    render(<PlanTree plan={PLAN} activeOperatorId="filter_1" />);
    expect(screen.getByRole("button", { name: /Filter filter_1/ })).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(
      screen.getByRole("button", { name: /SeqScan scan_1/ }),
    ).not.toHaveAttribute("aria-current");
  });

  it("selects an operator on click and deselects on a second click", async () => {
    const onSelectOperator = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <PlanTree plan={PLAN} onSelectOperator={onSelectOperator} />,
    );

    await user.click(screen.getByRole("button", { name: /Filter filter_1/ }));
    expect(onSelectOperator).toHaveBeenCalledWith("filter_1");

    rerender(
      <PlanTree
        plan={PLAN}
        onSelectOperator={onSelectOperator}
        selectedOperatorId="filter_1"
      />,
    );
    await user.click(screen.getByRole("button", { name: /Filter filter_1/ }));
    expect(onSelectOperator).toHaveBeenLastCalledWith(null);
  });

  it("renders a single-operator plan, as SELECT * produces", () => {
    // An identity projection is dropped by the planner, so the scan is the root.
    render(
      <PlanTree
        plan={plan("scan_1", [node("scan_1", "SeqScan", { detail: "table=users" })])}
      />,
    );
    expect(screen.getByText("SeqScan")).toBeInTheDocument();
    expect(screen.queryByText("Project")).not.toBeInTheDocument();
  });
});

// -- Milestone 6: estimates and alternatives ------------------------------

/** A plan the way Milestone 6 returns it: costed, with a rejected alternative. */
const COSTED: PlanModel = {
  root_id: "scan_1",
  nodes: [
    node("scan_1", "IndexScan", {
      detail: "index=users_age age = 30",
      estimated_rows: 20,
      estimated_cost: 26.2,
      estimated_io_cost: 24,
      estimated_cpu_cost: 2.2,
      output_rows: 20,
    }),
  ],
  alternatives: [
    {
      description: "Sequential scan of users",
      access_path: "PhysicalSeqScan",
      estimated_cost: 387,
      estimated_rows: 2000,
      chosen: false,
      rejected_because: "14.8x the cost of the chosen plan",
      index_name: null,
    },
    {
      description: "Index scan on users_age (age = 30)",
      access_path: "PhysicalIndexScan",
      estimated_cost: 26.2,
      estimated_rows: 20,
      chosen: true,
      rejected_because: "",
      index_name: "users_age",
    },
  ],
  rewrites: ["fold_constants"],
  estimated_cost: 26.3,
  statistics: {
    table_name: "users",
    row_count: 2000,
    page_count: 87,
    stale: false,
    gathered_at_ns: 1,
  },
};

describe("PlanTree estimates", () => {
  it("shows what the planner expected beside what happened", () => {
    render(<PlanTree plan={COSTED} />);
    expect(screen.getByText(/est 20/)).toBeInTheDocument();
  });

  it("says nothing when there is no estimate", () => {
    // A plan from before the planner existed, or an execution that never ran.
    render(<PlanTree plan={PLAN} />);
    expect(screen.queryByText(/est /)).not.toBeInTheDocument();
  });

  it("stays quiet when the estimate was close", () => {
    // A badge on every row would train you to ignore it.
    render(<PlanTree plan={COSTED} />);
    expect(screen.queryByText(/x off/)).not.toBeInTheDocument();
  });

  it("flags an estimate that was badly wrong", () => {
    // A bad row estimate is where a bad plan almost always comes from, so it is
    // the one thing worth interrupting the reader about.
    const wrong: PlanModel = {
      ...COSTED,
      nodes: [
        node("scan_1", "SeqScan", { estimated_rows: 10, output_rows: 1000 }),
      ],
    };
    render(<PlanTree plan={wrong} />);
    expect(screen.getByText(/100.0x off/)).toBeInTheDocument();
  });

  it("treats over- and under-estimating as equally wrong", () => {
    const over: PlanModel = {
      ...COSTED,
      nodes: [node("scan_1", "SeqScan", { estimated_rows: 1000, output_rows: 10 })],
    };
    render(<PlanTree plan={over} />);
    expect(screen.getByText(/100.0x off/)).toBeInTheDocument();
  });
});

describe("AlternativesPanel", () => {
  it("lists every path considered, not just the winner", () => {
    render(<AlternativesPanel plan={COSTED} />);
    expect(screen.getByText("Sequential scan of users")).toBeInTheDocument();
    expect(screen.getByText("Index scan on users_age (age = 30)")).toBeInTheDocument();
  });

  it("says why the loser lost", () => {
    render(<AlternativesPanel plan={COSTED} />);
    expect(
      screen.getByText(/14.8x the cost of the chosen plan/),
    ).toBeInTheDocument();
  });

  it("shows the cost of each", () => {
    render(<AlternativesPanel plan={COSTED} />);
    expect(screen.getByText("387.0")).toBeInTheDocument();
    expect(screen.getByText("26.2")).toBeInTheDocument();
  });

  it("names the rewrite rules that fired", () => {
    render(<AlternativesPanel plan={COSTED} />);
    expect(screen.getByText("fold_constants")).toBeInTheDocument();
  });

  it("reports the statistics the estimates came from", () => {
    render(<AlternativesPanel plan={COSTED} />);
    expect(screen.getByText(/users: 2,000 rows, 87 pages/)).toBeInTheDocument();
  });

  it("warns when the statistics are stale", () => {
    // Estimates are only as good as the numbers behind them, and ChenDB does
    // not refresh those on every write.
    const stale: PlanModel = {
      ...COSTED,
      statistics: { ...COSTED.statistics!, stale: true },
    };
    render(<AlternativesPanel plan={stale} />);
    expect(screen.getByText(/Stale:/)).toBeInTheDocument();
  });

  it("does not warn when they are fresh", () => {
    render(<AlternativesPanel plan={COSTED} />);
    expect(screen.queryByText(/Stale:/)).not.toBeInTheDocument();
  });

  it("says so when there was nothing to choose between", () => {
    render(<AlternativesPanel plan={PLAN} />);
    expect(screen.getByText("No alternatives")).toBeInTheDocument();
  });
});
