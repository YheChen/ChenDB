import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EventTimeline } from "./EventTimeline";
import type { TraceRecordModel } from "@/types/api";

function makeEvent(overrides: Partial<TraceRecordModel> = {}): TraceRecordModel {
  return {
    seq: 1,
    timestamp_ns: 1_700_000_000_000_000_000,
    category: "storage",
    level: "STORAGE",
    event_type: "PageReadEvent",
    event: { page_id: 3, file_offset: 12288, source: "disk", duration_ns: 4200 },
    ...overrides,
  };
}

const noop = {
  paused: false,
  onTogglePause: vi.fn(),
  onClear: vi.fn(),
  droppedByServer: 0,
  droppedByClient: 0,
  totalReceived: 0,
};

describe("EventTimeline", () => {
  it("guides the user when nothing has happened yet", () => {
    render(<EventTimeline {...noop} events={[]} connection="open" />);
    expect(screen.getByText("No events yet")).toBeInTheDocument();
    expect(screen.getByText(/Insert a row or open a page/)).toBeInTheDocument();
  });

  it("shows the connection state so a dead engine is obvious", () => {
    const { rerender } = render(
      <EventTimeline {...noop} events={[]} connection="open" />,
    );
    expect(screen.getByText("live")).toBeInTheDocument();

    rerender(<EventTimeline {...noop} events={[]} connection="closed" />);
    expect(screen.getByText("disconnected")).toBeInTheDocument();
  });

  it("renders an event with its sequence number and payload facts", () => {
    render(
      <EventTimeline
        {...noop}
        events={[makeEvent()]}
        connection="open"
        totalReceived={1}
      />,
    );

    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("PageRead")).toBeInTheDocument();
    expect(screen.getByText(/page_id=3/)).toBeInTheDocument();
    expect(screen.getByText(/file_offset=12288/)).toBeInTheDocument();
    // Durations are humanised rather than dumped as raw nanoseconds.
    expect(screen.getByText(/4\.2 µs/)).toBeInTheDocument();
  });

  it("orders newest first, because that is what you are watching", () => {
    render(
      <EventTimeline
        {...noop}
        connection="open"
        events={[makeEvent({ seq: 1 }), makeEvent({ seq: 2 }), makeEvent({ seq: 3 })]}
      />,
    );
    const rendered = screen.getAllByText(/^#\d+$/).map((node) => node.textContent);
    expect(rendered).toEqual(["#3", "#2", "#1"]);
  });

  it("filters by category", async () => {
    const user = userEvent.setup();
    render(
      <EventTimeline
        {...noop}
        connection="open"
        events={[
          makeEvent({ seq: 1, category: "storage", event_type: "PageReadEvent" }),
          makeEvent({ seq: 2, category: "record", event_type: "RecordInsertedEvent" }),
        ]}
      />,
    );

    expect(screen.getByText("PageRead")).toBeInTheDocument();
    await user.selectOptions(
      screen.getByLabelText("Filter events by category"),
      "record",
    );
    expect(screen.queryByText("PageRead")).not.toBeInTheDocument();
    expect(screen.getByText("RecordInserted")).toBeInTheDocument();
  });

  it("reports dropped events instead of pretending the feed is complete", () => {
    render(
      <EventTimeline
        {...noop}
        connection="open"
        events={[makeEvent()]}
        droppedByServer={120}
        droppedByClient={5}
      />,
    );
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent("125 events were dropped");
    expect(notice).toHaveTextContent("120 by the server's backpressure policy");
    expect(notice).toHaveTextContent("has gaps");
  });

  it("can be paused, and says so", async () => {
    const onTogglePause = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <EventTimeline
        {...noop}
        connection="open"
        events={[]}
        onTogglePause={onTogglePause}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(onTogglePause).toHaveBeenCalled();

    rerender(
      <EventTimeline
        {...noop}
        connection="open"
        events={[]}
        paused
        onTogglePause={onTogglePause}
      />,
    );
    expect(screen.getByRole("button", { name: "Resume" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });
});
