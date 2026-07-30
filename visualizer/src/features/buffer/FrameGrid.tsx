/**
 * The buffer pool as a grid of frames.
 *
 *   ┌────┬────┬────┬────┬────┬────┬────┬────┐
 *   │ p4 │ p5 │ p6*│ p7 │ p1 │ p2 │ p3 │    │   * = dirty
 *   └────┴────┴────┴────┴────┴────┴────┴────┘
 *     ▲                              ▲    ▲
 *   coldest, evicted next        hottest  free
 *
 * A cache is one of the few parts of a database whose behaviour is genuinely
 * *visual*: you can watch a working set settle in, and watch a sequential scan
 * wipe it out. That only works if the grid keeps a fixed shape, so free frames
 * are drawn too, and a page appearing reads as a change rather than as the whole
 * layout reflowing.
 *
 * Frames are shown in **frame order**, not recency order, for the same reason:
 * a frame is a physical slot, and watching one slot's contents get replaced is
 * the thing worth seeing. Recency is shown as colour instead, the coldest
 * resident frame is the one eviction takes next, and it is highlighted, because
 * "which one goes next" is the question the policy exists to answer.
 */

import { Badge, EmptyState } from "@/components/primitives";
import { cn, formatBytes, formatCount } from "@/lib/format";
import type { BufferPoolResponse, FrameModel } from "@/types/api";

/** Frames past this are summarised rather than drawn; 512 boxes helps nobody. */
const MAX_FRAMES_DRAWN = 256;

export function FrameGrid({
  pool,
  selectedPageId,
  onSelectPage,
}: {
  pool: BufferPoolResponse;
  selectedPageId?: number | null;
  onSelectPage?: (pageId: number) => void;
}) {
  const drawn = pool.frames.slice(0, MAX_FRAMES_DRAWN);
  const hidden = pool.frames.length - drawn.length;
  const coldest = coldestResident(pool.frames);

  if (pool.frames.length === 0) {
    return <EmptyState title="No frames" hint="The pool has no capacity." />;
  }

  return (
    <div className="space-y-2 p-3">
      <div
        className="grid gap-1"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(56px, 1fr))" }}
        role="list"
        aria-label={`${pool.capacity} buffer pool frames, ${pool.resident} resident`}
      >
        {drawn.map((frame) => (
          <Frame
            key={frame.frame_id}
            frame={frame}
            nextToGo={frame.page_id !== null && frame.page_id === coldest}
            selected={selectedPageId === frame.page_id}
            onSelect={onSelectPage}
          />
        ))}
      </div>

      {hidden > 0 ? (
        <p className="text-muted text-[10px]">
          …and {formatCount(hidden)} more frames not drawn.
        </p>
      ) : null}

      <div className="text-muted flex flex-wrap gap-3 text-[10px]">
        <Swatch className="bg-emerald-500/30" label="resident" />
        <Swatch className="bg-amber-500/40" label="dirty: the disk copy is stale" />
        <Swatch
          className="border border-[var(--accent)] bg-transparent"
          label="coldest: evicted next"
        />
        <Swatch className="bg-[var(--surface-sunken)]" label="free" />
      </div>
    </div>
  );
}

function Swatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className={cn("inline-block h-2.5 w-4 rounded-sm", className)} />
      {label}
    </span>
  );
}

/** The page eviction will take next: the resident frame with the highest rank. */
function coldestResident(frames: FrameModel[]): number | null {
  let worst: FrameModel | null = null;
  for (const frame of frames) {
    if (frame.page_id === null) continue;
    if (worst === null || frame.recency > worst.recency) worst = frame;
  }
  return worst?.page_id ?? null;
}

function Frame({
  frame,
  nextToGo,
  selected,
  onSelect,
}: {
  frame: FrameModel;
  nextToGo: boolean;
  selected: boolean;
  onSelect?: (pageId: number) => void;
}) {
  const free = frame.page_id === null;
  const label = free
    ? `frame ${frame.frame_id}, free`
    : `frame ${frame.frame_id}, page ${frame.page_id}, ` +
      `${frame.dirty ? "dirty" : "clean"}, ${frame.reads} read(s), ` +
      `recency ${frame.recency}${nextToGo ? ", evicted next" : ""}`;

  return (
    <button
      type="button"
      role="listitem"
      disabled={free}
      aria-label={label}
      aria-pressed={selected}
      title={label}
      onClick={() => frame.page_id !== null && onSelect?.(frame.page_id)}
      className={cn(
        "flex h-11 flex-col items-center justify-center rounded border text-[10px] transition-colors",
        free
          ? "surface-sunken border-[var(--border-subtle)] opacity-50"
          : frame.dirty
            ? "border-amber-500/50 bg-amber-500/25 hover:bg-amber-500/40"
            : "border-emerald-500/40 bg-emerald-500/20 hover:bg-emerald-500/35",
        nextToGo && "ring-2 ring-[var(--accent)] ring-inset",
        selected && "ring-2 ring-[var(--accent)]",
      )}
    >
      <span className="font-mono font-semibold">
        {free ? "—" : `p${frame.page_id}`}
      </span>
      {!free ? (
        <span className="text-muted font-mono text-[9px]">
          {frame.dirty ? "dirty" : `r${frame.reads}`}
        </span>
      ) : null}
    </button>
  );
}

/** Hit rate, residency and the write-back win, as headline numbers. */
export function PoolCounters({ pool }: { pool: BufferPoolResponse }) {
  const savedReads = pool.logical_reads - pool.physical_reads;
  const savedWrites = pool.logical_writes - pool.physical_writes;

  return (
    <div className="space-y-3 p-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        <Counter
          label="hit rate"
          value={`${(pool.stats.hit_rate * 100).toFixed(1)}%`}
          hint={`${formatCount(pool.stats.hits)} hits of ${formatCount(pool.stats.lookups)} lookups`}
        />
        <Counter
          label="resident"
          value={`${pool.resident} / ${pool.capacity}`}
          hint={`${formatBytes(pool.bytes_used)} of page images held in memory`}
        />
        <Counter
          label="evictions"
          value={formatCount(pool.stats.evictions)}
          hint={`${formatCount(pool.stats.dirty_evictions)} had to be written back first`}
        />
        <Counter
          label="dirty"
          value={formatCount(pool.dirty)}
          hint="Frames whose disk copy is stale. Zero after a sync."
        />
      </dl>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Saved
          label="reads"
          logical={pool.logical_reads}
          physical={pool.physical_reads}
          saved={savedReads}
          hint="Page reads the pool served without a syscall."
        />
        <Saved
          label="writes"
          logical={pool.logical_writes}
          physical={pool.physical_writes}
          saved={savedWrites}
          hint="Write-back: a page written many times reaches the disk once."
        />
      </div>
    </div>
  );
}

function Counter({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div title={hint}>
      <dt className="text-muted text-[10px] tracking-wide uppercase">{label}</dt>
      <dd className="font-mono text-sm font-semibold">{value}</dd>
    </div>
  );
}

function Saved({
  label,
  logical,
  physical,
  saved,
  hint,
}: {
  label: string;
  logical: number;
  physical: number;
  saved: number;
  hint: string;
}) {
  const fraction = logical > 0 ? Math.max(saved, 0) / logical : 0;
  return (
    <div className="surface-sunken rounded border border-[var(--border-subtle)] p-2">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[11px] font-medium">{label}</span>
        <span className="text-muted font-mono text-[10px]" title={hint}>
          {formatCount(logical)} asked · {formatCount(physical)} on disk
        </span>
      </div>
      <div className="surface h-2 overflow-hidden rounded-full">
        <div
          className="h-full rounded-full bg-emerald-500"
          style={{ width: `${fraction * 100}%` }}
        />
      </div>
      <p className="text-muted mt-1 text-[10px]">
        {saved > 0 ? (
          <>
            <Badge tone="accent">{(fraction * 100).toFixed(0)}%</Badge> avoided:{" "}
            {formatCount(saved)} {label} never reached the disk
          </>
        ) : (
          "nothing avoided yet"
        )}
      </p>
    </div>
  );
}
