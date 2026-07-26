/**
 * A two-pane split with a draggable divider.
 *
 * Written by hand rather than pulled in as a dependency: the whole component is
 * one pointer handler plus a percentage, and it needs keyboard support that
 * most small libraries skip. Arrow keys move the divider, so the layout is
 * reachable without a mouse.
 */

import { useCallback, useId, useRef, useState } from "react";
import { cn } from "@/lib/format";

const KEYBOARD_STEP_PERCENT = 4;

export function SplitPane({
  direction = "horizontal",
  initialPercent = 50,
  minPercent = 15,
  maxPercent = 85,
  first,
  second,
  className,
  label,
}: {
  direction?: "horizontal" | "vertical";
  initialPercent?: number;
  minPercent?: number;
  maxPercent?: number;
  first: React.ReactNode;
  second: React.ReactNode;
  className?: string;
  label: string;
}) {
  const [percent, setPercent] = useState(initialPercent);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const id = useId();
  const isHorizontal = direction === "horizontal";

  const clamp = useCallback(
    (value: number) => Math.min(maxPercent, Math.max(minPercent, value)),
    [minPercent, maxPercent],
  );

  const onPointerMove = useCallback(
    (event: PointerEvent) => {
      if (!dragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const ratio = isHorizontal
        ? (event.clientX - rect.left) / rect.width
        : (event.clientY - rect.top) / rect.height;
      setPercent(clamp(ratio * 100));
    },
    [clamp, isHorizontal],
  );

  const stopDragging = useCallback(() => {
    dragging.current = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", stopDragging);
  }, [onPointerMove]);

  const startDragging = useCallback(() => {
    dragging.current = true;
    // Set on the body so the cursor stays consistent even when the pointer
    // leaves the divider mid-drag.
    document.body.style.cursor = isHorizontal ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopDragging);
  }, [isHorizontal, onPointerMove, stopDragging]);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      const decrease = isHorizontal ? "ArrowLeft" : "ArrowUp";
      const increase = isHorizontal ? "ArrowRight" : "ArrowDown";
      if (event.key === decrease) {
        setPercent((current) => clamp(current - KEYBOARD_STEP_PERCENT));
      } else if (event.key === increase) {
        setPercent((current) => clamp(current + KEYBOARD_STEP_PERCENT));
      } else if (event.key === "Home") {
        setPercent(minPercent);
      } else if (event.key === "End") {
        setPercent(maxPercent);
      } else {
        return;
      }
      event.preventDefault();
    },
    [clamp, isHorizontal, minPercent, maxPercent],
  );

  return (
    <div
      ref={containerRef}
      className={cn("flex min-h-0 min-w-0", isHorizontal ? "flex-row" : "flex-col", className)}
    >
      <div
        className="flex min-h-0 min-w-0"
        style={{ flexBasis: `${percent}%`, flexGrow: 0, flexShrink: 0 }}
        id={`${id}-first`}
      >
        {first}
      </div>
      <div
        role="separator"
        aria-orientation={isHorizontal ? "vertical" : "horizontal"}
        aria-label={label}
        aria-valuenow={Math.round(percent)}
        aria-valuemin={minPercent}
        aria-valuemax={maxPercent}
        aria-controls={`${id}-first`}
        tabIndex={0}
        onPointerDown={startDragging}
        onKeyDown={onKeyDown}
        className={cn(
          "group relative shrink-0 bg-transparent transition-colors",
          isHorizontal
            ? "w-1.5 cursor-col-resize hover:bg-[var(--accent)]/30"
            : "h-1.5 cursor-row-resize hover:bg-[var(--accent)]/30",
        )}
      >
        <span
          aria-hidden
          className={cn(
            "absolute bg-[var(--border-subtle)]",
            isHorizontal
              ? "top-0 bottom-0 left-1/2 w-px -translate-x-1/2"
              : "top-1/2 right-0 left-0 h-px -translate-y-1/2",
          )}
        />
      </div>
      <div className="flex min-h-0 min-w-0 flex-1">{second}</div>
    </div>
  );
}
