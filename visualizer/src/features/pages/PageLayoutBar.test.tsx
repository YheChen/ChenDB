import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PageLayoutBar } from "./PageLayoutBar";
import type { PageDetailModel } from "@/types/api";

/**
 * A realistic heap page: 256 bytes, two live records and one tombstone,
 * matching what the engine produces for a small demo table.
 */
function makePage(overrides: Partial<PageDetailModel> = {}): PageDetailModel {
  return {
    summary: {
      page_id: 2,
      page_type: "HEAP",
      file_offset: 512,
      lsn: 0,
      checksum: 123,
      checksum_valid: true,
      slot_count: 3,
      live_record_count: 2,
      free_space: 160,
      reclaimable_space: 20,
      next_page_id: null,
      owner: "users",
      error: null,
      dirty: false,
    },
    header_fields: [],
    slots: [
      { slot_id: 0, offset: 226, length: 30, is_live: true, raw_hex: "00".repeat(30) },
      { slot_id: 1, offset: 0, length: 0, is_live: false, raw_hex: "" },
      { slot_id: 2, offset: 196, length: 30, is_live: true, raw_hex: "11".repeat(30) },
    ],
    raw_hex: "00".repeat(256),
    page_size: 256,
    header_size: 24,
    slot_directory_end: 36,
    free_start: 36,
    free_end: 196,
    ...overrides,
  };
}

describe("PageLayoutBar", () => {
  it("labels every region of the slotted page", () => {
    render(
      <PageLayoutBar page={makePage()} selectedSlotId={null} onSelectSlot={vi.fn()} />,
    );

    for (const region of ["Header", "Slot directory", "Free space", "Record data"]) {
      expect(screen.getByText(region)).toBeInTheDocument();
    }
  });

  it("shows each region's real byte range", () => {
    render(
      <PageLayoutBar page={makePage()} selectedSlotId={null} onSelectSlot={vi.fn()} />,
    );

    // The four regions must tile the page exactly: 0-24, 24-36, 36-196, 196-256.
    expect(screen.getByText(/\[0, 24\)/)).toBeInTheDocument();
    expect(screen.getByText(/\[24, 36\)/)).toBeInTheDocument();
    expect(screen.getByText(/\[36, 196\)/)).toBeInTheDocument();
    expect(screen.getByText(/\[196, 256\)/)).toBeInTheDocument();
  });

  it("renders one clickable block per live record and skips tombstones", () => {
    render(
      <PageLayoutBar page={makePage()} selectedSlotId={null} onSelectSlot={vi.fn()} />,
    );

    expect(screen.getByTitle(/Slot 0: offset 226, 30 bytes/)).toBeInTheDocument();
    expect(screen.getByTitle(/Slot 2: offset 196, 30 bytes/)).toBeInTheDocument();
    expect(screen.queryByTitle(/Slot 1:/)).not.toBeInTheDocument();
  });

  it("selects a slot on click and deselects it on a second click", async () => {
    const onSelectSlot = vi.fn();
    const user = userEvent.setup();

    const { rerender } = render(
      <PageLayoutBar page={makePage()} selectedSlotId={null} onSelectSlot={onSelectSlot} />,
    );
    await user.click(screen.getByTitle(/Slot 0:/));
    expect(onSelectSlot).toHaveBeenCalledWith(0);

    rerender(
      <PageLayoutBar page={makePage()} selectedSlotId={0} onSelectSlot={onSelectSlot} />,
    );
    await user.click(screen.getByTitle(/Slot 0:/));
    expect(onSelectSlot).toHaveBeenLastCalledWith(null);
  });

  it("marks the selected slot for assistive technology", () => {
    render(
      <PageLayoutBar page={makePage()} selectedSlotId={2} onSelectSlot={vi.fn()} />,
    );
    expect(screen.getByTitle(/Slot 2:/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTitle(/Slot 0:/)).toHaveAttribute("aria-pressed", "false");
  });

  it("omits the record strip when the page is empty", () => {
    const empty = makePage({
      slots: [],
      slot_directory_end: 24,
      free_start: 24,
      free_end: 256,
    });
    render(<PageLayoutBar page={empty} selectedSlotId={null} onSelectSlot={vi.fn()} />);

    expect(screen.queryByText(/click to inspect/)).not.toBeInTheDocument();
    expect(screen.getByText("Free space")).toBeInTheDocument();
  });

  it("describes the page for screen readers", () => {
    render(
      <PageLayoutBar page={makePage()} selectedSlotId={null} onSelectSlot={vi.fn()} />,
    );
    expect(
      screen.getByRole("img", { name: "Page 2 layout, 256 bytes" }),
    ).toBeInTheDocument();
  });
});
