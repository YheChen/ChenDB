/**
 * Small shared UI primitives.
 *
 * Kept in one file on purpose: each is a handful of lines, and a developer tool
 * needs a consistent dense look far more than it needs a component library.
 */

import type { ReactNode } from "react";
import { cn } from "@/lib/format";

// -- panel ------------------------------------------------------------------

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={cn(
        "surface flex min-h-0 flex-col overflow-hidden rounded-lg border",
        className,
      )}
    >
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border-subtle)] px-3 py-2">
        <div className="min-w-0">
          <h2 className="truncate text-xs font-semibold tracking-wide uppercase">
            {title}
          </h2>
          {subtitle ? (
            <p className="text-muted truncate text-[11px]">{subtitle}</p>
          ) : null}
        </div>
        {actions ? (
          <div className="flex shrink-0 items-center gap-1.5">{actions}</div>
        ) : null}
      </header>
      <div className={cn("scroll-thin min-h-0 flex-1 overflow-auto", bodyClassName)}>
        {children}
      </div>
    </section>
  );
}

// -- button -----------------------------------------------------------------

type ButtonVariant = "default" | "primary" | "danger" | "ghost";

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  default:
    "border border-[var(--border-subtle)] hover:bg-[var(--surface-sunken)]",
  primary:
    "border border-transparent bg-[var(--accent)] text-white hover:opacity-90",
  danger:
    "border border-transparent bg-red-600 text-white hover:bg-red-700",
  ghost: "border border-transparent hover:bg-[var(--surface-sunken)]",
};

export function Button({
  variant = "default",
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      type="button"
      {...props}
      className={cn(
        "rounded px-2 py-1 text-xs font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-40",
        BUTTON_VARIANTS[variant],
        className,
      )}
    />
  );
}

// -- badge ------------------------------------------------------------------

const BADGE_TONES = {
  neutral: "bg-[var(--surface-sunken)] text-[var(--text-secondary)]",
  meta: "bg-violet-500/15 text-violet-600 dark:text-violet-300",
  heap: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  schema: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  free: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-300",
  danger: "bg-red-500/15 text-red-600 dark:text-red-300",
  accent: "bg-[var(--accent)]/15 text-[var(--accent)]",
} as const;

export type BadgeTone = keyof typeof BADGE_TONES;

export function Badge({
  tone = "neutral",
  children,
  className,
  title,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[10px] font-medium",
        BADGE_TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** Map a page type or owner onto a consistent colour across every panel. */
export function toneForPageType(pageType: string): BadgeTone {
  if (pageType.startsWith("META")) return "meta";
  if (pageType.startsWith("HEAP")) return "heap";
  if (pageType.startsWith("SCHEMA")) return "schema";
  if (pageType.startsWith("FREE")) return "free";
  return "danger";
}

// -- states -----------------------------------------------------------------

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
      <p className="text-sm font-medium">{title}</p>
      {hint ? <p className="text-muted max-w-sm text-xs">{hint}</p> : null}
      {action}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="text-muted flex h-full items-center justify-center gap-2 p-6 text-xs"
    >
      <span
        aria-hidden
        className="size-3 animate-spin rounded-full border-2 border-current border-t-transparent"
      />
      {label}…
    </div>
  );
}

export function ErrorNotice({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
      <p className="text-sm font-medium text-red-600 dark:text-red-400">
        Something went wrong
      </p>
      <p className="text-muted max-w-md text-xs">{message}</p>
      {onRetry ? (
        <Button onClick={onRetry} variant="default">
          Retry
        </Button>
      ) : null}
    </div>
  );
}

// -- field ------------------------------------------------------------------

/** A label/value pair for dense metadata grids. */
export function Field({
  label,
  value,
  title,
  mono = true,
}: {
  label: string;
  value: ReactNode;
  title?: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0" title={title}>
      <dt className="text-muted text-[10px] tracking-wide uppercase">{label}</dt>
      <dd className={cn("truncate text-xs", mono && "font-mono")}>{value}</dd>
    </div>
  );
}
