/** Formatting helpers shared across panels. Pure functions, easy to test. */

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB"] as const;
const NS_PER_MS = 1_000_000;

/** Human-readable byte count. Binary units, because pages are powers of two. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes)) return "—";
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1;
  return `${value.toFixed(digits)} ${BYTE_UNITS[unit]}`;
}

/** Nanoseconds at a sensible scale. Storage timings span ns to seconds. */
export function formatDuration(ns: number): string {
  if (!Number.isFinite(ns) || ns < 0) return "—";
  if (ns < 1_000) return `${ns} ns`;
  if (ns < 1_000_000) return `${(ns / 1_000).toFixed(1)} µs`;
  if (ns < 1_000_000_000) return `${(ns / 1_000_000).toFixed(2)} ms`;
  return `${(ns / 1_000_000_000).toFixed(2)} s`;
}

/** Thousands separators, so page and row counts stay readable. */
export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

/**
 * Time of day from an epoch-nanosecond timestamp.
 *
 * Events carry nanoseconds because storage operations are measured in
 * microseconds; `Date` only takes milliseconds, so the fractional part is
 * rendered explicitly rather than rounded away.
 */
export function formatTimestamp(ns: number): string {
  const date = new Date(Math.floor(ns / NS_PER_MS));
  const clock = date.toLocaleTimeString("en-US", { hour12: false });
  const millis = String(date.getMilliseconds()).padStart(3, "0");
  return `${clock}.${millis}`;
}

/** Zero-padded hex, e.g. 0x0000c70f. */
export function formatHex(value: number, width = 8): string {
  return `0x${(value >>> 0).toString(16).padStart(width, "0")}`;
}

/** Split a hex string into byte pairs for display. */
export function hexBytes(hex: string): string[] {
  const out: string[] = [];
  for (let i = 0; i + 1 < hex.length; i += 2) out.push(hex.slice(i, i + 2));
  return out;
}

/** Printable ASCII for a hexdump's right-hand column; '.' for anything else. */
export function hexToAscii(hex: string): string {
  return hexBytes(hex)
    .map((pair) => {
      const code = Number.parseInt(pair, 16);
      return code >= 0x20 && code < 0x7f ? String.fromCharCode(code) : ".";
    })
    .join("");
}

/** Render a decoded SQL value for a results cell. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return value;
  return String(value);
}

/** Percentage of a whole, clamped for layout bars. */
export function percentOf(part: number, whole: number): number {
  if (whole <= 0) return 0;
  return Math.max(0, Math.min(100, (part / whole) * 100));
}

/** Join class names, dropping falsy entries. */
export function cn(...values: (string | false | null | undefined)[]): string {
  return values.filter(Boolean).join(" ");
}
