import { describe, expect, it } from "vitest";
import {
  cn,
  formatBytes,
  formatCount,
  formatDuration,
  formatHex,
  formatValue,
  hexBytes,
  hexToAscii,
  percentOf,
} from "./format";

describe("formatBytes", () => {
  it("uses binary units, because pages are powers of two", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(256)).toBe("256 B");
    expect(formatBytes(4096)).toBe("4.0 KiB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MiB");
  });

  it("drops the decimal once the number is large enough to not need it", () => {
    expect(formatBytes(1024 * 150)).toBe("150 KiB");
  });

  it("survives nonsense rather than rendering NaN", () => {
    expect(formatBytes(Number.NaN)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("picks a scale that keeps storage timings readable", () => {
    expect(formatDuration(512)).toBe("512 ns");
    expect(formatDuration(2_500)).toBe("2.5 µs");
    expect(formatDuration(3_400_000)).toBe("3.40 ms");
    expect(formatDuration(2_000_000_000)).toBe("2.00 s");
  });

  it("rejects negatives instead of showing them", () => {
    expect(formatDuration(-1)).toBe("—");
  });
});

describe("formatValue", () => {
  it("renders NULL distinctly from an empty string", () => {
    expect(formatValue(null)).toBe("NULL");
    expect(formatValue("")).toBe("");
  });

  it("renders booleans as SQL-ish literals", () => {
    expect(formatValue(true)).toBe("true");
    expect(formatValue(false)).toBe("false");
  });

  it("keeps zero visible", () => {
    expect(formatValue(0)).toBe("0");
    expect(formatValue(0.0)).toBe("0");
  });
});

describe("hex helpers", () => {
  it("splits a hex string into byte pairs", () => {
    expect(hexBytes("deadbeef")).toEqual(["de", "ad", "be", "ef"]);
    expect(hexBytes("")).toEqual([]);
  });

  it("renders printable ASCII and dots for the rest", () => {
    // "ChenDB" followed by NUL and 0xff, as the magic actually appears on disk.
    expect(hexToAscii("4368656e444200ff")).toBe("ChenDB..");
  });

  it("zero-pads hex to a fixed width", () => {
    expect(formatHex(0xc70f)).toBe("0x0000c70f");
    expect(formatHex(255, 2)).toBe("0xff");
  });
});

describe("percentOf", () => {
  it("clamps rather than overflowing a layout bar", () => {
    expect(percentOf(50, 100)).toBe(50);
    expect(percentOf(150, 100)).toBe(100);
    expect(percentOf(-5, 100)).toBe(0);
  });

  it("treats a zero total as empty instead of dividing by zero", () => {
    expect(percentOf(10, 0)).toBe(0);
  });
});

describe("misc", () => {
  it("formats counts with separators", () => {
    expect(formatCount(1234567)).toBe("1,234,567");
  });

  it("joins class names and drops falsy entries", () => {
    expect(cn("a", false, undefined, "b", null)).toBe("a b");
  });
});
