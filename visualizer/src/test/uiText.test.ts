/**
 * Text the user reads must not claim the engine cannot do things it can.
 *
 * Six strings had gone stale by Milestone 16, and every one of them read as a
 * confident statement of fact:
 *
 *     SqlWorkspace     "Milestone 2 parses; nothing executes yet (that is
 *                       Milestone 3)"                shipped in Milestone 3
 *     PageInspector    "Always 0 until the write-ahead log arrives in
 *                       Milestone 9"                 shipped in Milestone 9
 *     RecordsPanel     "There is no buffer pool yet" shipped in Milestone 7
 *     RecordsPanel     "No index exists until Milestone 5"
 *     ResultsPanel     "No buffer pool yet"
 *     ResultsPanel     "No index exists until Milestone 5"
 *
 * They survived because a tooltip is written once, while it is true, and then
 * nobody reads it again. Milestone 12's demo-SQL guard covers SQL in the
 * catalogue; none of these were SQL.
 *
 * ## How this reads the source
 *
 * Comments are stripped first, and that division carries the whole test. A
 * comment saying "the buffer pool arrives in Milestone 7" is *documentation of
 * history* and belongs there. The same sentence in a `title=` is a lie told to
 * a user. Nothing else distinguishes them, which is why the stripper below is
 * hand-written rather than a regex: it has to respect strings and template
 * literals or it would eat half the file.
 *
 * ## What it does and does not catch
 *
 * The **milestone rule is general**: user-facing text may reference a milestone
 * in the past, never as pending. That is mechanical and will catch phrasings
 * nobody has thought of yet.
 *
 * The **absence rule is a deny-list**, and deliberately not general. "No
 * transactions yet", "No events yet" and "No tables yet" are all legitimate
 * empty states, so a rule against "no X yet" would fire on five correct
 * strings. Each entry below is a claim that was true once and is false now.
 * It is not exhaustive and cannot be; it is a regression test for six specific
 * lies plus the shape they share.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SOURCE_ROOT = "src";

/** Generated from the server's schema; its descriptions are not UI copy. */
const NOT_UI = ["types/api.ts"];

/**
 * Remove `//` and block comments, leaving string and template literals intact.
 *
 * A regex cannot do this: `"http://example.com"` contains `//`, and a template
 * literal can contain both comment forms as ordinary text. So this walks the
 * file once, and the only states it needs are "in a string" and "in a comment".
 */
export function stripComments(text: string): string {
  let out = "";
  let i = 0;
  const n = text.length;

  while (i < n) {
    const c = text[i];
    const d = text[i + 1];

    if (c === "/" && d === "/") {
      while (i < n && text[i] !== "\n") i++;
      continue;
    }
    if (c === "/" && d === "*") {
      i += 2;
      while (i < n && !(text[i] === "*" && text[i + 1] === "/")) i++;
      i += 2;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      const quote = c;
      out += c;
      i++;
      while (i < n) {
        if (text[i] === "\\") {
          out += text[i] + (text[i + 1] ?? "");
          i += 2;
          continue;
        }
        out += text[i];
        if (text[i] === quote) {
          i++;
          break;
        }
        i++;
      }
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

function sourceFiles(dir: string, found: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      sourceFiles(full, found);
    } else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) {
      if (!NOT_UI.some((skip) => full.endsWith(skip))) found.push(full);
    }
  }
  return found;
}

type Hit = { file: string; line: number; text: string; why: string };

function scan(patterns: { pattern: RegExp; why: string }[]): Hit[] {
  const hits: Hit[] = [];
  for (const file of sourceFiles(SOURCE_ROOT)) {
    const lines = stripComments(readFileSync(file, "utf8")).split("\n");
    lines.forEach((line, index) => {
      for (const { pattern, why } of patterns) {
        if (pattern.test(line)) {
          hits.push({ file, line: index + 1, text: line.trim().slice(0, 120), why });
        }
      }
    });
  }
  return hits;
}

const report = (hits: Hit[]) =>
  hits.map((h) => `\n  ${h.file}:${h.line}\n    ${h.text}\n    → ${h.why}`).join("");

describe("user-facing text", () => {
  it("never defers to a milestone", () => {
    // The shape all six shared: naming a milestone as somewhere the feature
    // still lives. A *past* reference is fine and one exists on purpose —
    // "Before Milestone 8 they would have stayed" is the point of that demo.
    const hits = scan([
      {
        pattern: /\b(until|that is|arrives? in|comes? in|coming in|lands? in|added in)\s+Milestone\s*\d+/i,
        why: "reads as pending. If it has shipped, describe what it does; if not, hide the control.",
      },
      {
        pattern: /\bMilestone\s*\d+\s+(will|adds|brings|is where|introduces)\b/i,
        why: "reads as pending, same as above.",
      },
    ]);
    expect(hits, `text that defers to a milestone:${report(hits)}`).toEqual([]);
  });

  it("never claims a shipped subsystem is missing", () => {
    // Not a general rule — see the header. Each of these was true once.
    const hits = scan([
      { pattern: /\bno buffer pool\b/i, why: "the buffer pool shipped in Milestone 7." },
      { pattern: /\bno index exists\b/i, why: "indexes shipped in Milestone 5." },
      { pattern: /\bnothing executes\b/i, why: "the executor shipped in Milestone 3." },
      { pattern: /\bno (write-ahead log|wal)\b/i, why: "the WAL shipped in Milestone 9." },
      {
        pattern: /\bno (planner|cost model)\b/i,
        why: "the planner shipped in Milestone 6.",
      },
      {
        pattern: /\b(cannot|can't|does not|doesn't) (execute|run) (sql|queries|statements)\b/i,
        why: "it can, since Milestone 3.",
      },
      {
        pattern: /\b(update|delete|join|group by) is not (implemented|supported)\b/i,
        why: "UPDATE and DELETE shipped in Milestone 11, joins and GROUP BY in Milestone 13.",
      },
    ]);
    expect(hits, `text claiming a shipped feature is absent:${report(hits)}`).toEqual([]);
  });

  it("is being read at all", () => {
    // A guard that silently stops finding files is worse than no guard. These
    // are the files most likely to carry stale copy, so their absence means the
    // walk broke rather than the tree being clean.
    const files = sourceFiles(SOURCE_ROOT);
    expect(files.length).toBeGreaterThan(40);
    for (const expected of [
      "src/features/pages/PageInspector.tsx",
      "src/features/execution/ResultsPanel.tsx",
      "src/features/records/RecordsPanel.tsx",
      "src/features/sql/SqlWorkspace.tsx",
      "src/lib/demoSql.ts",
    ]) {
      expect(files).toContain(expected);
    }
  });
});

describe("the comment stripper", () => {
  // The division between comment and string is what the whole test rests on,
  // so it is checked rather than assumed.
  it("removes both comment forms", () => {
    expect(stripComments("a // gone\nb")).toBe("a \nb");
    expect(stripComments("a /* gone */ b")).toBe("a  b");
  });

  it("keeps a // that is inside a string", () => {
    expect(stripComments('const u = "http://x.com";')).toContain("http://x.com");
    expect(stripComments("const u = 'a // b';")).toContain("a // b");
  });

  it("keeps comment syntax inside a template literal", () => {
    const source = "const t = `see // and /* this */`;";
    expect(stripComments(source)).toContain("see // and /* this */");
  });

  it("is not fooled by an escaped quote", () => {
    const source = 'const s = "a \\" // still in the string"; // gone';
    const out = stripComments(source);
    expect(out).toContain("still in the string");
    expect(out).not.toContain("gone");
  });

  it("would have caught the bug this file exists for", () => {
    const source = 'title="Always 0 until the write-ahead log arrives in Milestone 9."';
    expect(
      /\b(until|arrives? in)\s+Milestone\s*\d+/i.test(stripComments(source)),
    ).toBe(true);
    // …and not the comment that legitimately explains the same history.
    const comment = "// the write-ahead log arrives in Milestone 9, and until then";
    expect(
      /\b(until|arrives? in)\s+Milestone\s*\d+/i.test(stripComments(comment)),
    ).toBe(false);
  });
});
