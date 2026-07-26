/**
 * The B+ tree, drawn as it actually is.
 *
 *                 ┌───┬────┬────┐
 *                 │-∞ │ 40 │ 80 │            internal: separators + children
 *                 └─┬─┴──┬─┴──┬─┘
 *          ┌────────┘    │    └────────┐
 *      ┌───┴───┐     ┌───┴───┐     ┌───┴───┐
 *      │10│20│30│╌╌╌▶│40│55│70│╌╌╌▶│80│90│99│   leaves, chained
 *      └───────┘     └───────┘     └───────┘
 *
 * Two things a generic tree component gets wrong, and the reason this is
 * hand-drawn rather than delegated to one:
 *
 * **A node holds many keys, not one.** A binary-tree renderer draws a circle per
 * node with one label. A B+ tree node is a *row of cells*, and its width depends
 * on how many keys it holds — which is the whole visual point, because a node
 * filling up is what precedes a split.
 *
 * **Leaves are linked, and that link is not a tree edge.** It runs sideways
 * between siblings that may have different parents. Drawn dashed, so it reads
 * as a different kind of relationship from the parent-child arrows.
 *
 * Layout is computed here rather than with d3-hierarchy for the same reason:
 * d3's tidy-tree assumes uniform node widths and only draws tree edges, so
 * bending it to this shape is more code than placing the boxes directly. Levels
 * are evenly spaced vertically; within a level, nodes are spread by their own
 * widths. That is O(nodes) and produces no crossings, because the API returns
 * nodes in breadth-first order and a B+ tree's children never interleave.
 *
 * Width is the hard part. A real index is *wide*: 600 rows on a 512-byte page
 * is thirty leaves, and drawing every key of every one is tens of thousands of
 * pixels. Two things keep it legible:
 *
 * * **the cell budget shrinks as a level gets wider**, eliding the middle of
 *   each node and keeping the first and last keys — the two that actually bound
 *   the subtree;
 * * **the default is natural size and a horizontal scroll**, so keys stay
 *   readable, with a "Fit" zoom for when the shape matters more than the
 *   contents. A traced lookup scrolls its leaf into view, so searching a wide
 *   tree does not mean hunting for the highlight.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/format";
import type { TreeNodeModel, TreeSnapshotModel } from "@/types/api";

const CELL_WIDTH = 46;
const CELL_HEIGHT = 26;
const NODE_GAP = 22;
const LEVEL_GAP = 78;
const PADDING = 20;

/** Cells per node, by how many nodes share the widest level. */
const CELL_BUDGETS: [nodesInLevel: number, cells: number][] = [
  [8, 9],
  [16, 6],
  [40, 4],
  [Infinity, 3],
];

const ZOOMS = [
  { label: "Fit", value: 0 },
  { label: "1x", value: 1 },
  { label: "2x", value: 2 },
] as const;

const DEFAULT_ZOOM = 1;

type Placed = {
  node: TreeNodeModel;
  x: number;
  y: number;
  width: number;
  cells: string[];
  overflow: number;
};

/**
 * A node's rendered cells. Long nodes are elided in the middle rather than
 * clipped: the first and last keys are the ones that bound the subtree, so they
 * are exactly the ones worth keeping.
 */
function cellsFor(
  node: TreeNodeModel,
  budget: number,
): { cells: string[]; overflow: number } {
  if (node.keys.length <= budget) return { cells: node.keys, overflow: 0 };
  const head = node.keys.slice(0, Math.max(budget - 2, 1));
  const tail = node.keys.slice(-1);
  return {
    cells: [...head, "…", ...tail],
    overflow: node.keys.length - head.length - tail.length,
  };
}

function budgetFor(widestLevel: number): number {
  return CELL_BUDGETS.find(([limit]) => widestLevel <= limit)![1];
}

function layout(nodes: TreeNodeModel[]): { placed: Placed[]; width: number; height: number } {
  const byLevel = new Map<number, TreeNodeModel[]>();
  for (const node of nodes) {
    const bucket = byLevel.get(node.level);
    if (bucket) bucket.push(node);
    else byLevel.set(node.level, [node]);
  }

  const budget = budgetFor(Math.max(...[...byLevel.values()].map((l) => l.length)));
  const levels = [...byLevel.keys()].sort((a, b) => b - a); // root first
  const placed: Placed[] = [];
  let widest = 0;

  levels.forEach((level, row) => {
    const inLevel = byLevel.get(level)!;
    const measured = inLevel.map((node) => {
      const { cells, overflow } = cellsFor(node, budget);
      return {
        node,
        cells,
        overflow,
        width: Math.max(cells.length, 1) * CELL_WIDTH,
      };
    });
    const total =
      measured.reduce((sum, entry) => sum + entry.width, 0) +
      NODE_GAP * Math.max(measured.length - 1, 0);
    widest = Math.max(widest, total);

    let cursor = 0;
    for (const entry of measured) {
      placed.push({
        ...entry,
        x: cursor,
        y: PADDING + row * LEVEL_GAP,
      });
      cursor += entry.width + NODE_GAP;
    }
  });

  // Centre each level against the widest one, so the tree reads as symmetric
  // even when a level is short.
  levels.forEach((level) => {
    const inLevel = placed.filter((entry) => entry.node.level === level);
    const total =
      inLevel.reduce((sum, entry) => sum + entry.width, 0) +
      NODE_GAP * Math.max(inLevel.length - 1, 0);
    const offset = (widest - total) / 2;
    for (const entry of inLevel) entry.x += offset + PADDING;
  });

  return {
    placed,
    width: widest + PADDING * 2,
    height: PADDING * 2 + Math.max(levels.length - 1, 0) * LEVEL_GAP + CELL_HEIGHT,
  };
}

export function BTreeView({
  tree,
  highlightedPath = [],
  selectedPageId,
  onSelectPage,
}: {
  tree: TreeSnapshotModel;
  /** Page ids on the path a traced lookup took, root to leaf. */
  highlightedPath?: number[];
  selectedPageId?: number | null;
  onSelectPage?: (pageId: number) => void;
}) {
  const [zoom, setZoom] = useState<number>(DEFAULT_ZOOM);
  const scroller = useRef<HTMLDivElement>(null);
  const { placed, width, height } = useMemo(() => layout(tree.nodes), [tree.nodes]);
  const positions = useMemo(
    () => new Map(placed.map((entry) => [entry.node.page_id, entry])),
    [placed],
  );
  const onPath = useMemo(() => new Set(highlightedPath), [highlightedPath]);

  // Centre the leaf a lookup reached, or the root when nothing is highlighted.
  // Both matter on a wide tree: without the first, searching highlights a node
  // off the right edge and looks like nothing happened; without the second, the
  // view opens at scroll zero with the root — dead centre — out of sight.
  const target = highlightedPath.at(-1) ?? tree.root_page_id;
  useEffect(() => {
    const container = scroller.current;
    if (container === null || target === undefined) return;
    const leaf = positions.get(target);
    if (leaf === undefined) return;
    const scale = zoom === 0 ? container.clientWidth / width : zoom;
    container.scrollLeft =
      (leaf.x + leaf.width / 2) * scale - container.clientWidth / 2;
  }, [target, positions, zoom, width]);

  if (tree.nodes.length === 0) {
    return <p className="text-muted p-3 text-xs">This index has no nodes to show.</p>;
  }

  return (
    <div className="min-h-0">
      <div className="flex items-center justify-end gap-1 px-3 pt-1">
        <span className="text-muted mr-auto font-mono text-[10px]">
          {width}x{height} px at 1x
        </span>
        {ZOOMS.map((option) => (
          <button
            key={option.label}
            type="button"
            aria-pressed={zoom === option.value}
            onClick={() => setZoom(option.value)}
            className={cn(
              "rounded border border-[var(--border)] px-1.5 py-0.5 text-[10px]",
              zoom === option.value
                ? "bg-[var(--accent)] text-white"
                : "hover:bg-[var(--surface-sunken)]",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
      <div ref={scroller} className="overflow-auto p-2">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="xMidYMin meet"
          role="img"
          aria-label={`B+ tree with ${tree.nodes.length} node(s), height ${tree.height}`}
          // Zoom 0 means "fit": the viewBox scales the drawing down to the
          // panel width and the height follows the aspect ratio. Any other
          // value is a multiple of the natural pixel size, and the container
          // scrolls.
          style={
            zoom === 0
              ? { width: "100%", height: "auto" }
              : { width: width * zoom, height: height * zoom, maxWidth: "none" }
          }
        >
          <g>
            {placed.map((parent) =>
              parent.node.children.map((childId, position) => {
                const child = positions.get(childId);
                if (!child) return null;
                const active = onPath.has(parent.node.page_id) && onPath.has(childId);
                // Leave from under the separator that routes to this child, so
                // the arrow visibly starts at the key it belongs to.
                const fromX =
                  parent.x + Math.min(position + 0.5, parent.cells.length) * CELL_WIDTH;
                return (
                  <path
                    key={`${parent.node.page_id}-${childId}`}
                    d={edge(fromX, parent.y + CELL_HEIGHT, child.x + child.width / 2, child.y)}
                    fill="none"
                    stroke={active ? "var(--accent)" : "var(--border)"}
                    strokeWidth={active ? 2 : 1}
                  />
                );
              }),
            )}
          </g>

          <g>
            {placed.map((entry) => {
              const next = entry.node.next_leaf_id;
              if (next === null) return null;
              const sibling = positions.get(next);
              if (!sibling) return null;
              return (
                <line
                  key={`leaf-${entry.node.page_id}`}
                  x1={entry.x + entry.width}
                  y1={entry.y + CELL_HEIGHT / 2}
                  x2={sibling.x}
                  y2={sibling.y + CELL_HEIGHT / 2}
                  stroke="var(--accent)"
                  strokeWidth={1}
                  strokeDasharray="3 3"
                  opacity={0.55}
                />
              );
            })}
          </g>

          {placed.map((entry) => (
            <TreeNode
              key={entry.node.page_id}
              entry={entry}
              onPath={onPath.has(entry.node.page_id)}
              selected={selectedPageId === entry.node.page_id}
              onSelect={onSelectPage}
            />
          ))}
        </svg>
      </div>
    </div>
  );
}

/** A cubic curve, so edges are readable when a node has many children. */
function edge(x1: number, y1: number, x2: number, y2: number): string {
  const midY = (y1 + y2) / 2;
  return `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`;
}

function TreeNode({
  entry,
  onPath,
  selected,
  onSelect,
}: {
  entry: Placed;
  onPath: boolean;
  selected: boolean;
  onSelect?: (pageId: number) => void;
}) {
  const { node, x, y, width, cells, overflow } = entry;
  const label =
    `page ${node.page_id}, ${node.is_leaf ? "leaf" : "internal"}, ` +
    `${node.entry_count} entr${node.entry_count === 1 ? "y" : "ies"}, ` +
    `${node.free_bytes} bytes free`;

  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={label}
      aria-pressed={selected}
      onClick={() => onSelect?.(node.page_id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect?.(node.page_id);
        }
      }}
      className={cn("cursor-pointer", onSelect ? "hover:opacity-80" : "")}
    >
      <title>{label}</title>
      <rect
        x={x}
        y={y}
        width={width}
        height={CELL_HEIGHT}
        rx={3}
        className={node.is_leaf ? "fill-emerald-500/10" : "fill-sky-500/10"}
        stroke={
          selected || onPath ? "var(--accent)" : "var(--border)"
        }
        strokeWidth={selected ? 2.5 : onPath ? 2 : 1}
      />
      {cells.map((key, position) => (
        <g key={position}>
          {position > 0 ? (
            <line
              x1={x + position * CELL_WIDTH}
              y1={y}
              x2={x + position * CELL_WIDTH}
              y2={y + CELL_HEIGHT}
              stroke="var(--border-subtle)"
              strokeWidth={1}
            />
          ) : null}
          <text
            x={x + position * CELL_WIDTH + CELL_WIDTH / 2}
            y={y + CELL_HEIGHT / 2 + 4}
            textAnchor="middle"
            className="fill-[var(--text)] font-mono text-[10px]"
          >
            {truncate(key)}
          </text>
        </g>
      ))}
      <text
        x={x + width / 2}
        y={y - 5}
        textAnchor="middle"
        className="fill-[var(--text-muted)] font-mono text-[9px]"
      >
        p{node.page_id}
        {overflow > 0 ? ` (+${overflow})` : ""}
      </text>
    </g>
  );
}

function truncate(key: string): string {
  return key.length <= 6 ? key : `${key.slice(0, 5)}…`;
}
