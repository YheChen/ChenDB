import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PlanTree } from "./PlanTree";
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
    next_calls: 0,
    input_rows: 0,
    output_rows: 0,
    rows_rejected: 0,
    duration_ns: 1000,
    ...overrides,
  };
}

/** Project ← Filter ← SeqScan, with the statistics a real run produces. */
const PLAN: PlanModel = {
  root_id: "project_1",
  nodes: [
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
  ],
};

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
        plan={{ root_id: "scan_1", nodes: [node("scan_1", "SeqScan", { detail: "table=users" })] }}
      />,
    );
    expect(screen.getByText("SeqScan")).toBeInTheDocument();
    expect(screen.queryByText("Project")).not.toBeInTheDocument();
  });
});
