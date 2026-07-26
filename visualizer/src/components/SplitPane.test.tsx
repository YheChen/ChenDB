import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SplitPane } from "./SplitPane";

function renderPane(props: Partial<Parameters<typeof SplitPane>[0]> = {}) {
  return render(
    <SplitPane
      label="Resize panels"
      first={<div>left</div>}
      second={<div>right</div>}
      {...props}
    />,
  );
}

describe("SplitPane", () => {
  it("renders both panes", () => {
    renderPane();
    expect(screen.getByText("left")).toBeInTheDocument();
    expect(screen.getByText("right")).toBeInTheDocument();
  });

  it("exposes the divider as a labelled separator", () => {
    renderPane({ initialPercent: 40 });
    const separator = screen.getByRole("separator", { name: "Resize panels" });
    expect(separator).toHaveAttribute("aria-valuenow", "40");
    expect(separator).toHaveAttribute("aria-orientation", "vertical");
  });

  it("is resizable from the keyboard", async () => {
    const user = userEvent.setup();
    renderPane({ initialPercent: 50 });
    const separator = screen.getByRole("separator");

    separator.focus();
    await user.keyboard("{ArrowRight}");
    expect(separator).toHaveAttribute("aria-valuenow", "54");

    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(separator).toHaveAttribute("aria-valuenow", "46");
  });

  it("clamps to the configured bounds", async () => {
    const user = userEvent.setup();
    renderPane({ initialPercent: 50, minPercent: 30, maxPercent: 70 });
    const separator = screen.getByRole("separator");

    separator.focus();
    await user.keyboard("{End}");
    expect(separator).toHaveAttribute("aria-valuenow", "70");

    await user.keyboard("{ArrowRight}");
    expect(separator).toHaveAttribute("aria-valuenow", "70");

    await user.keyboard("{Home}");
    expect(separator).toHaveAttribute("aria-valuenow", "30");
  });

  it("uses the horizontal arrow keys when stacked vertically", async () => {
    const user = userEvent.setup();
    renderPane({ direction: "vertical", initialPercent: 50 });
    const separator = screen.getByRole("separator");
    expect(separator).toHaveAttribute("aria-orientation", "horizontal");

    separator.focus();
    await user.keyboard("{ArrowDown}");
    expect(separator).toHaveAttribute("aria-valuenow", "54");
  });
});
