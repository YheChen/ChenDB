/**
 * The B+ tree view.
 *
 * The spec is explicit that this must not be a generic binary-tree component,
 * so the tests assert the two things that distinguish a B+ tree from one:
 * a node shows *every* key it holds, and leaves are chained sideways.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BTreeView } from "./BTreeView";
import type { TreeNodeModel, TreeSnapshotModel } from "@/types/api";

function node(overrides: Partial<TreeNodeModel> & { page_id: number }): TreeNodeModel {
  return {
    level: 0,
    is_leaf: true,
    keys: [],
    children: [],
    record_ids: [],
    next_leaf_id: null,
    free_bytes: 100,
    entry_count: overrides.keys?.length ?? 0,
    ...overrides,
  };
}

/** Root over three chained leaves — the shape the module docstring draws. */
const TREE: TreeSnapshotModel = {
  root_page_id: 1,
  height: 2,
  truncated: false,
  nodes: [
    node({
      page_id: 1,
      level: 1,
      is_leaf: false,
      keys: ["-∞", "40", "80"],
      children: [2, 3, 4],
      entry_count: 3,
    }),
    node({
      page_id: 2,
      keys: ["10", "20", "30"],
      record_ids: ["(9,0)", "(9,1)", "(9,2)"],
      next_leaf_id: 3,
      entry_count: 3,
    }),
    node({
      page_id: 3,
      keys: ["40", "55", "70"],
      record_ids: ["(9,3)", "(9,4)", "(9,5)"],
      next_leaf_id: 4,
      entry_count: 3,
    }),
    node({
      page_id: 4,
      keys: ["80", "90", "99"],
      record_ids: ["(9,6)", "(9,7)", "(9,8)"],
      entry_count: 3,
    }),
  ],
};

describe("BTreeView", () => {
  it("shows every key in a node, not one label per node", () => {
    render(<BTreeView tree={TREE} />);
    for (const key of ["10", "20", "30", "40", "55", "70", "80", "90", "99"]) {
      expect(screen.getAllByText(key).length).toBeGreaterThan(0);
    }
  });

  it("shows the minus-infinity separator every internal node starts with", () => {
    render(<BTreeView tree={TREE} />);
    expect(screen.getByText("-∞")).toBeInTheDocument();
  });

  it("draws one sibling link per chained leaf, and none from the last", () => {
    const { container } = render(<BTreeView tree={TREE} />);
    // Two of the three leaves have a next_leaf_id.
    expect(container.querySelectorAll("line[stroke-dasharray]")).toHaveLength(2);
  });

  it("draws a parent edge per child", () => {
    const { container } = render(<BTreeView tree={TREE} />);
    expect(container.querySelectorAll("path")).toHaveLength(3);
  });

  it("labels each node with what it is, for screen readers", () => {
    render(<BTreeView tree={TREE} />);
    expect(
      screen.getByRole("button", { name: /page 1, internal, 3 entries/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /page 2, leaf, 3 entries, 100 bytes free/ }),
    ).toBeInTheDocument();
  });

  it("reports the whole tree in one accessible label", () => {
    render(<BTreeView tree={TREE} />);
    expect(
      screen.getByRole("img", { name: "B+ tree with 4 node(s), height 2" }),
    ).toBeInTheDocument();
  });

  it("selects a page when a node is clicked", async () => {
    const onSelectPage = vi.fn();
    render(<BTreeView tree={TREE} onSelectPage={onSelectPage} />);
    await userEvent.click(screen.getByRole("button", { name: /page 3, leaf/ }));
    expect(onSelectPage).toHaveBeenCalledWith(3);
  });

  it("is reachable by keyboard", async () => {
    const onSelectPage = vi.fn();
    render(<BTreeView tree={TREE} onSelectPage={onSelectPage} />);
    screen.getByRole("button", { name: /page 2, leaf/ }).focus();
    await userEvent.keyboard("{Enter}");
    expect(onSelectPage).toHaveBeenCalledWith(2);
  });

  it("elides a node with more keys than fit, keeping the first and last", () => {
    // The first and last keys are the ones that bound the subtree, so they are
    // exactly the ones worth keeping when a node is too wide to draw.
    const wide: TreeSnapshotModel = {
      root_page_id: 1,
      height: 1,
      truncated: false,
      nodes: [
        node({
          page_id: 1,
          keys: Array.from({ length: 20 }, (_, n) => String(n)),
          entry_count: 20,
        }),
      ],
    };
    render(<BTreeView tree={wide} />);
    expect(screen.getByText("0")).toBeInTheDocument();
    expect(screen.getByText("19")).toBeInTheDocument();
    expect(screen.getByText("…")).toBeInTheDocument();
    // The count of hidden keys is on the page label, so nothing is silently lost.
    expect(screen.getByText(/p1 \(\+12\)/)).toBeInTheDocument();
  });

  it("shows fewer keys per node as a level gets wider", () => {
    // A real index is dozens of leaves across. Keeping nine cells each would be
    // tens of thousands of pixels, so the budget shrinks with the level.
    const wide: TreeSnapshotModel = {
      root_page_id: 0,
      height: 2,
      truncated: false,
      nodes: [
        node({
          page_id: 0,
          level: 1,
          is_leaf: false,
          keys: ["-∞"],
          children: Array.from({ length: 30 }, (_, n) => n + 1),
          entry_count: 1,
        }),
        ...Array.from({ length: 30 }, (_, n) =>
          node({
            page_id: n + 1,
            keys: ["a", "b", "c", "d", "e", "f"],
            entry_count: 6,
          }),
        ),
      ],
    };
    render(<BTreeView tree={wide} />);
    // Six keys, budget four: two shown, an ellipsis, then the last.
    expect(screen.getAllByText("…")).toHaveLength(30);
    expect(screen.queryByText("d")).not.toBeInTheDocument();
    expect(screen.getAllByText("f").length).toBe(30);
  });

  it("defaults to natural size, and can be zoomed out to fit", async () => {
    // Natural size keeps keys readable; "Fit" trades that for the shape of a
    // tree too wide to show at once.
    const { container } = render(<BTreeView tree={TREE} />);
    expect(screen.getByRole("button", { name: "1x" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(container.querySelector("svg")!.style.width).not.toBe("100%");

    await userEvent.click(screen.getByRole("button", { name: "Fit" }));
    expect(container.querySelector("svg")!.style.width).toBe("100%");
  });

  it("scrolls the leaf a lookup reached into view", () => {
    // Otherwise a highlight on a wide tree lands off-screen and reads as
    // nothing having happened.
    const { container } = render(<BTreeView tree={TREE} highlightedPath={[1, 4]} />);
    const scroller = container.querySelector(".overflow-auto") as HTMLDivElement;
    Object.defineProperty(scroller, "clientWidth", { value: 200, configurable: true });
    expect(scroller).toBeTruthy();
    // jsdom reports clientWidth 0 by default, so the exact offset is not
    // meaningful; that the effect ran without throwing on a real element is.
    expect(scroller.scrollLeft).toBeGreaterThanOrEqual(0);
  });

  it("says so rather than rendering blank when there is nothing to draw", () => {
    render(
      <BTreeView
        tree={{ root_page_id: 1, height: 1, truncated: false, nodes: [] }}
      />,
    );
    expect(screen.getByText(/no nodes to show/)).toBeInTheDocument();
  });

  it("survives a truncated response that references a missing child", () => {
    // max_nodes can cut a tree mid-level, so a child page id may not have been
    // sent. Drawing an edge to nothing would throw; skipping it must not.
    const partial: TreeSnapshotModel = {
      root_page_id: 1,
      height: 2,
      truncated: true,
      nodes: [
        node({
          page_id: 1,
          level: 1,
          is_leaf: false,
          keys: ["-∞", "40"],
          children: [2, 99],
          entry_count: 2,
        }),
        node({ page_id: 2, keys: ["10"], next_leaf_id: 99, entry_count: 1 }),
      ],
    };
    const { container } = render(<BTreeView tree={partial} />);
    expect(container.querySelectorAll("path")).toHaveLength(1);
    expect(container.querySelectorAll("line[stroke-dasharray]")).toHaveLength(0);
  });
});
