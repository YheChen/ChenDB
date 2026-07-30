/**
 * The frame grid.
 *
 * The grid's whole job is to make the cache legible, so the tests assert the
 * three things that carry meaning: free frames are drawn (so the grid keeps a
 * fixed shape), dirty frames are distinguishable, and the frame eviction will
 * take next is marked, because "which one goes next" is the question the
 * policy exists to answer.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FrameGrid, PoolCounters } from "./FrameGrid";
import type { BufferPoolResponse, FrameModel } from "@/types/api";

function frame(overrides: Partial<FrameModel> & { frame_id: number }): FrameModel {
  return {
    page_id: null,
    dirty: false,
    reads: 0,
    writes: 0,
    recency: -1,
    resident_for_ns: 0,
    ...overrides,
  };
}

function pool(overrides: Partial<BufferPoolResponse> = {}): BufferPoolResponse {
  return {
    capacity: 4,
    page_size: 4096,
    resident: 3,
    dirty: 1,
    bytes_used: 3 * 4096,
    frames: [
      frame({ frame_id: 0, page_id: 7, recency: 0, reads: 5 }),
      frame({ frame_id: 1, page_id: 8, recency: 2, reads: 1 }),
      frame({ frame_id: 2, page_id: 9, recency: 1, dirty: true, writes: 3 }),
      frame({ frame_id: 3 }),
    ],
    stats: {
      hits: 90,
      misses: 10,
      lookups: 100,
      hit_rate: 0.9,
      evictions: 6,
      dirty_evictions: 2,
      writes_absorbed: 40,
      flushes: 1,
      pages_flushed: 3,
    },
    logical_reads: 100,
    physical_reads: 10,
    logical_writes: 50,
    physical_writes: 10,
    ...overrides,
  };
}

describe("FrameGrid", () => {
  it("draws every frame, including the free ones", () => {
    // A fixed shape means a page appearing reads as a change, not a reflow.
    render(<FrameGrid pool={pool()} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getByRole("listitem", { name: /frame 3, free/ })).toBeInTheDocument();
  });

  it("shows which page is in which frame", () => {
    render(<FrameGrid pool={pool()} />);
    expect(screen.getByText("p7")).toBeInTheDocument();
    expect(screen.getByText("p9")).toBeInTheDocument();
  });

  it("labels a dirty frame as dirty", () => {
    render(<FrameGrid pool={pool()} />);
    expect(
      screen.getByRole("listitem", { name: /page 9, dirty/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: /page 7, clean/ }),
    ).toBeInTheDocument();
  });

  it("marks the frame eviction will take next", () => {
    // The highest recency rank among resident frames, page 8 here.
    render(<FrameGrid pool={pool()} />);
    expect(
      screen.getByRole("listitem", { name: /page 8.*evicted next/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("listitem", { name: /page 7.*evicted next/ }),
    ).not.toBeInTheDocument();
  });

  it("opens a page in the inspector when a frame is clicked", () => {
    const onSelectPage = vi.fn();
    render(<FrameGrid pool={pool()} onSelectPage={onSelectPage} />);
    return userEvent
      .click(screen.getByRole("listitem", { name: /page 9/ }))
      .then(() => expect(onSelectPage).toHaveBeenCalledWith(9));
  });

  it("does not offer a free frame as clickable", async () => {
    const onSelectPage = vi.fn();
    render(<FrameGrid pool={pool()} onSelectPage={onSelectPage} />);
    const free = screen.getByRole("listitem", { name: /frame 3, free/ });
    expect(free).toBeDisabled();
    await userEvent.click(free);
    expect(onSelectPage).not.toHaveBeenCalled();
  });

  it("summarises frames it does not draw rather than dropping them", () => {
    const many = pool({
      capacity: 400,
      frames: Array.from({ length: 400 }, (_, n) => frame({ frame_id: n })),
    });
    render(<FrameGrid pool={many} />);
    expect(screen.getByText(/144 more frames not drawn/)).toBeInTheDocument();
  });

  it("says so rather than rendering nothing when there is no pool", () => {
    render(<FrameGrid pool={pool({ capacity: 0, frames: [] })} />);
    expect(screen.getByText("No frames")).toBeInTheDocument();
  });

  it("reports the whole grid in one accessible label", () => {
    render(<FrameGrid pool={pool()} />);
    expect(
      screen.getByRole("list", { name: "4 buffer pool frames, 3 resident" }),
    ).toBeInTheDocument();
  });
});

describe("PoolCounters", () => {
  it("leads with the hit rate", () => {
    render(<PoolCounters pool={pool()} />);
    expect(screen.getByText("90.0%")).toBeInTheDocument();
  });

  it("shows residency against capacity", () => {
    render(<PoolCounters pool={pool()} />);
    expect(screen.getByText("3 / 4")).toBeInTheDocument();
  });

  it("shows how much I/O never happened", () => {
    // 100 logical reads, 10 physical: 90 syscalls the pool absorbed.
    render(<PoolCounters pool={pool()} />);
    expect(screen.getByText(/90 reads never reached the disk/)).toBeInTheDocument();
    expect(screen.getByText(/40 writes never reached the disk/)).toBeInTheDocument();
  });

  it("says nothing was saved rather than showing a misleading zero", () => {
    const cold = pool({
      logical_reads: 10,
      physical_reads: 10,
      logical_writes: 5,
      physical_writes: 5,
    });
    render(<PoolCounters pool={cold} />);
    expect(screen.getAllByText("nothing avoided yet")).toHaveLength(2);
  });

  it("survives a pool that has done nothing at all", () => {
    const fresh = pool({
      resident: 0,
      dirty: 0,
      bytes_used: 0,
      logical_reads: 0,
      physical_reads: 0,
      logical_writes: 0,
      physical_writes: 0,
      stats: { ...pool().stats, hits: 0, misses: 0, lookups: 0, hit_rate: 0 },
    });
    render(<PoolCounters pool={fresh} />);
    expect(screen.getByText("0.0%")).toBeInTheDocument();
  });
});
