/**
 * The log table and the recovery report.
 *
 * Each panel exists to make one thing legible, so the tests assert those:
 *
 * * the log is ordered forwards, marks what is not yet on disk, and says so
 *   when it is showing a window rather than everything;
 * * the recovery report distinguishes "clean shutdown" from "nothing was
 *   wrong", and gives "already current" the same billing as "replayed" —
 *   because a skipped record is the last checkpoint paying for itself.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RecoveryPanel } from "./RecoveryPanel";
import { WalCounters, WalTable } from "./WalTable";
import type {
  RecoveryReportModel,
  WalRecordModel,
  WalResponse,
} from "@/types/api";

function record(
  overrides: Partial<WalRecordModel> & { lsn: number },
): WalRecordModel {
  return {
    prev_lsn: 0,
    transaction_id: 1,
    record_type: "update",
    page_id: 4,
    size: 556,
    before_image_size: 0,
    after_image_size: 512,
    ...overrides,
  };
}

function wal(overrides: Partial<WalResponse> = {}): WalResponse {
  return {
    enabled: true,
    path: "demo.chendb-wal",
    base_lsn: 0,
    next_lsn: 1112,
    flushed_lsn: 1112,
    buffered_bytes: 0,
    size_bytes: 1112,
    records: [record({ lsn: 0 }), record({ lsn: 556, prev_lsn: 0 })],
    truncated_tail: false,
    total_records: 2,
    stats: {
      records_appended: 2,
      records_coalesced: 0,
      bytes_appended: 1112,
      flushes: 1,
      syncs: 1,
      mean_sync_ns: 60_000,
      checkpoints: 0,
      bytes_reclaimed: 0,
    },
    ...overrides,
  };
}

function report(
  overrides: Partial<RecoveryReportModel> = {},
): RecoveryReportModel {
  return {
    ran: true,
    records_scanned: 40,
    truncated_tail: false,
    winners: [1],
    losers: [2],
    pages_redone: 12,
    pages_skipped: 25,
    pages_undone: 3,
    highest_lsn: 9000,
    duration_ns: 4_000_000,
    phase_ns: { analysis: 100_000, redo: 3_000_000, undo: 900_000 },
    summary: "recovered 40 record(s): 12 redone, 25 already current, 3 undone",
    ...overrides,
  };
}

describe("WalTable", () => {
  it("says an empty log is the normal state, not a problem", () => {
    render(<WalTable wal={wal({ records: [], total_records: 0 })} />);
    expect(
      screen.getByText(/clean shutdown or a fresh checkpoint/i),
    ).toBeInTheDocument();
  });

  it("hides itself when the database has no log", () => {
    render(<WalTable wal={wal({ enabled: false })} />);
    expect(screen.getByText(/no log/i)).toBeInTheDocument();
  });

  it("lists records forwards, because that is replay order", () => {
    render(<WalTable wal={wal()} />);
    const lsns = screen.getAllByText(/^(0|556)$/).map((n) => n.textContent);
    expect(lsns.slice(0, 2)).toEqual(["0", "556"]);
  });

  it("distinguishes a record that carries undo from one that does not", () => {
    render(
      <WalTable
        wal={wal({
          records: [record({ lsn: 0, before_image_size: 512 })],
          total_records: 1,
        })}
      />,
    );
    expect(screen.getAllByText("512 B").length).toBeGreaterThan(0);
  });

  it("marks staged records as not yet on the disk", () => {
    render(<WalTable wal={wal({ flushed_lsn: 0, buffered_bytes: 1112 })} />);
    expect(screen.getByText(/still staged in memory/i)).toBeInTheDocument();
    expect(
      screen.getByText(/would lose exactly that much/i),
    ).toBeInTheDocument();
  });

  it("reports a torn tail as expected rather than as corruption", () => {
    render(<WalTable wal={wal({ truncated_tail: true })} />);
    expect(
      screen.getByText(/died part-way through writing it/i),
    ).toBeInTheDocument();
  });

  it("says when it is showing a window rather than everything", () => {
    render(<WalTable wal={wal({ total_records: 5000 })} />);
    expect(
      screen.getByText(/showing the last 2 of 5,000/i),
    ).toBeInTheDocument();
  });

  it("opens a page in the inspector when asked", async () => {
    const onSelectPage = vi.fn();
    render(
      <WalTable
        wal={wal({
          records: [record({ lsn: 0, page_id: 12 })],
          total_records: 1,
        })}
        onSelectPage={onSelectPage}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "12" }));
    expect(onSelectPage).toHaveBeenCalledWith(12);
  });

  it("shows a bookkeeping record as belonging to no transaction", () => {
    render(
      <WalTable
        wal={wal({
          records: [record({ lsn: 0, transaction_id: 0 })],
          total_records: 1,
        })}
      />,
    );
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});

describe("WalCounters", () => {
  it("turns the fsync into a commit ceiling", () => {
    // 60 µs per fsync is about 16,667 commits a second, and that number has
    // nothing to do with how much work each transaction did.
    render(<WalCounters wal={wal()} />);
    expect(screen.getByText("60 µs")).toBeInTheDocument();
    expect(screen.getByText("16,667/s")).toBeInTheDocument();
  });

  it("reports coalescing as a percentage of writes avoided", () => {
    render(
      <WalCounters
        wal={wal({
          stats: {
            ...wal().stats,
            records_appended: 10,
            records_coalesced: 90,
          },
        })}
      />,
    );
    expect(screen.getByText("90%")).toBeInTheDocument();
  });
});

describe("RecoveryPanel", () => {
  it("distinguishes a clean shutdown from a clean recovery", () => {
    render(<RecoveryPanel report={report({ ran: false })} />);
    expect(screen.getByText(/nothing to recover/i)).toBeInTheDocument();
    expect(screen.getByText(/ends with a checkpoint/i)).toBeInTheDocument();
  });

  it("shows the three phases in order", () => {
    render(<RecoveryPanel report={report()} />);
    for (const phase of ["analysis", "redo", "undo"]) {
      expect(screen.getByText(phase)).toBeInTheDocument();
    }
  });

  it("names the interrupted transactions", () => {
    render(<RecoveryPanel report={report({ losers: [2, 5] })} />);
    expect(screen.getByText(/interrupted #2, #5/)).toBeInTheDocument();
  });

  it("gives skipped records equal billing with replayed ones", () => {
    render(<RecoveryPanel report={report()} />);
    expect(screen.getByText("12 replayed")).toBeInTheDocument();
    // Twice: once in the engine's own summary line, once in the redo phase row.
    expect(screen.getAllByText(/25 already current/)).toHaveLength(2);
  });

  it("says so when there was nothing to undo", () => {
    render(<RecoveryPanel report={report({ losers: [], pages_undone: 0 })} />);
    expect(screen.getByText(/every transaction finished/i)).toBeInTheDocument();
  });
});
