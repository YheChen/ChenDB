import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PageListPanel } from "./PageListPanel";
import type { PageListResponse, PageSummaryModel } from "@/types/api";

function summary(overrides: Partial<PageSummaryModel> = {}): PageSummaryModel {
  return {
    page_id: 0,
    page_type: "META",
    file_offset: 0,
    lsn: 0,
    checksum: 1,
    checksum_valid: true,
    slot_count: 0,
    live_record_count: 0,
    free_space: 196,
    reclaimable_space: 0,
    next_page_id: null,
    owner: "meta",
    error: null,
    dirty: false,
    ...overrides,
  };
}

const RESPONSE: PageListResponse = {
  page_size: 256,
  page_count: 3,
  total_bytes: 768,
  pages: [
    summary(),
    summary({ page_id: 1, page_type: "SCHEMA", owner: "schema", file_offset: 256 }),
    summary({
      page_id: 2,
      page_type: "HEAP",
      owner: "users",
      file_offset: 512,
      slot_count: 5,
      live_record_count: 4,
      free_space: 20,
    }),
  ],
};

function renderPanel(props: Partial<Parameters<typeof PageListPanel>[0]> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PageListPanel
        databaseId="demo"
        selectedPageId={null}
        onSelectPage={() => undefined}
        {...props}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(RESPONSE), { status: 200 })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PageListPanel", () => {
  it("prompts when no database is open", () => {
    renderPanel({ databaseId: null });
    expect(screen.getByText("No database open")).toBeInTheDocument();
  });

  it("summarises the file above the list", async () => {
    renderPanel();
    expect(await screen.findByText(/3 pages/)).toBeInTheDocument();
    expect(screen.getByText(/256 B\/page/)).toBeInTheDocument();
  });

  it("names every page row for assistive technology", async () => {
    renderPanel();
    // Each row is entirely visual — a badge, a fill bar, a count — so the
    // accessible name has to be supplied explicitly.
    expect(
      await screen.findByRole("button", {
        name: "Inspect page 2, HEAP, owned by users",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Inspect page 0, META, owned by meta" }),
    ).toBeInTheDocument();
  });

  it("reports each page's fill from its real free space", async () => {
    renderPanel();
    // Page 2: 20 of 256 bytes free, so 236 are used.
    expect(await screen.findByTitle("236 B of 256 B used")).toBeInTheDocument();
  });

  it("selects a page on click", async () => {
    const onSelectPage = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onSelectPage });

    await user.click(
      await screen.findByRole("button", { name: /Inspect page 2/ }),
    );
    expect(onSelectPage).toHaveBeenCalledWith(2);
  });

  it("marks the selected page as current", async () => {
    renderPanel({ selectedPageId: 1 });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Inspect page 1/ }),
      ).toHaveAttribute("aria-current", "true"),
    );
    expect(
      screen.getByRole("button", { name: /Inspect page 0/ }),
    ).not.toHaveAttribute("aria-current");
  });

  it("flags a page whose checksum does not verify", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              ...RESPONSE,
              pages: [summary({ page_id: 0, checksum_valid: false })],
            }),
            { status: 200 },
          ),
      ),
    );
    renderPanel();
    expect(await screen.findByText("CRC")).toBeInTheDocument();
  });

  it("surfaces a dead engine instead of an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network down");
      }),
    );
    renderPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Cannot reach the engine/,
    );
  });
});
