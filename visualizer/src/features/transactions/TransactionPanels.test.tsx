/**
 * The undo log and the timeline.
 *
 * Both panels exist to teach one thing each, so the tests assert exactly those:
 *
 * * the undo log shows *pages*, newest first, and marks the one a rollback
 *   would restore first;
 * * the timeline distinguishes implicit from explicit transactions, the single
 *   most surprising thing about how the engine behaves after Milestone 8, and
 *   invisible without the label.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TransactionTimeline } from "./TransactionTimeline";
import { UndoLogPanel } from "./UndoLogPanel";
import type { TransactionModel, UndoRecordModel } from "@/types/api";

function record(
  overrides: Partial<UndoRecordModel> & { sequence: number },
): UndoRecordModel {
  return {
    page_id: overrides.sequence + 1,
    before_image_size: 4096,
    reason: "insert",
    ...overrides,
  };
}

function transaction(
  overrides: Partial<TransactionModel> & { transaction_id: number },
): TransactionModel {
  return {
    state: "committed",
    implicit: true,
    statements: 1,
    pages_written: 2,
    pages_held: 0,
    pages_restored: 0,
    undo_bytes: 0,
    duration_ns: 1_000_000,
    records: [],
    ...overrides,
  };
}

describe("UndoLogPanel", () => {
  it("says what an empty log means rather than showing an empty table", () => {
    render(<UndoLogPanel records={[]} pageSize={4096} />);
    expect(screen.getByText(/nothing to undo/i)).toBeInTheDocument();
    expect(screen.getByText(/captured once/i)).toBeInTheDocument();
  });

  it("lists newest first, because that is rollback order", () => {
    render(
      <UndoLogPanel
        records={[
          record({ sequence: 0 }),
          record({ sequence: 1 }),
          record({ sequence: 2 }),
        ]}
        pageSize={4096}
      />,
    );
    const pages = screen
      .getAllByText(/^page \d+$/)
      .map((node) => node.textContent);
    expect(pages).toEqual(["page 3", "page 2", "page 1"]);
  });

  it("marks the image a rollback writes back first", () => {
    render(
      <UndoLogPanel
        records={[record({ sequence: 0 }), record({ sequence: 1 })]}
        pageSize={4096}
      />,
    );
    expect(screen.getByText(/first/)).toBeInTheDocument();
  });

  it("shows each image's size, which is always one page", () => {
    render(
      <UndoLogPanel records={[record({ sequence: 0 })]} pageSize={4096} />,
    );
    expect(screen.getByText("4.0 KiB")).toBeInTheDocument();
  });

  it("names what captured each page", () => {
    render(
      <UndoLogPanel
        records={[record({ sequence: 0, reason: "index split" })]}
        pageSize={4096}
      />,
    );
    expect(screen.getByText("index split")).toBeInTheDocument();
  });

  it("opens a page in the inspector when asked", async () => {
    const onSelectPage = vi.fn();
    render(
      <UndoLogPanel
        records={[record({ sequence: 0, page_id: 12 })]}
        pageSize={4096}
        onSelectPage={onSelectPage}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "page 12" }));
    expect(onSelectPage).toHaveBeenCalledWith(12);
  });

  it("summarises rather than drawing an unbounded list", () => {
    const many = Array.from({ length: 260 }, (_, i) => record({ sequence: i }));
    render(<UndoLogPanel records={many} pageSize={4096} />);
    expect(screen.getByText(/60 older before-images/)).toBeInTheDocument();
  });
});

describe("TransactionTimeline", () => {
  it("explains that statements are transactional even with no history", () => {
    render(<TransactionTimeline transactions={[]} historyLimit={50} />);
    expect(screen.getByText(/without BEGIN/i)).toBeInTheDocument();
  });

  it("distinguishes implicit from explicit", () => {
    render(
      <TransactionTimeline
        transactions={[
          transaction({ transaction_id: 1 }),
          transaction({ transaction_id: 2, implicit: false }),
        ]}
        historyLimit={50}
      />,
    );
    expect(screen.getByText("implicit")).toBeInTheDocument();
    expect(screen.getByText("explicit")).toBeInTheDocument();
  });

  it("reports restored pages for an abort and held pages while open", () => {
    render(
      <TransactionTimeline
        transactions={[
          transaction({
            transaction_id: 1,
            state: "aborted",
            pages_restored: 6,
          }),
          transaction({ transaction_id: 2, state: "active", pages_held: 4 }),
        ]}
        historyLimit={50}
      />,
    );
    expect(screen.getByText("6 restored")).toBeInTheDocument();
    expect(screen.getByText("4 held")).toBeInTheDocument();
  });

  it("shows a failed transaction as failed, not as active", () => {
    render(
      <TransactionTimeline
        transactions={[
          transaction({ transaction_id: 1, state: "failed", pages_held: 2 }),
        ]}
        historyLimit={50}
      />,
    );
    expect(screen.getByText("failed")).toBeInTheDocument();
  });

  it("says the history is capped rather than implying it is complete", () => {
    const many = Array.from({ length: 50 }, (_, i) =>
      transaction({ transaction_id: i + 1 }),
    );
    render(<TransactionTimeline transactions={many} historyLimit={50} />);
    expect(screen.getByText(/only the last 50/i)).toBeInTheDocument();
  });
});
