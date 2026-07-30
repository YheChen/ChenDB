/**
 * The entry point for the build with no server behind it.
 *
 * Starting the engine takes a few seconds and about 15 MB, so this renders a
 * progress screen first and mounts the app only once the transport is ready.
 * The alternative, mount immediately and let every panel show its own
 * "cannot reach the engine", would be technically fine and read as broken.
 */

import { StrictMode, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { App } from "./App";
import { setTransport } from "./lib/transport";
import {
  clearStoredData,
  createWasmTransport,
  type BootProgress,
} from "./lib/wasmTransport";

function Booting({ progress }: { progress: BootProgress }) {
  return (
    <div className="flex h-dvh flex-col items-center justify-center gap-4 px-6 font-mono">
      <p className="text-sm">
        ChenDB <span className="text-muted">· the whole engine, in this tab</span>
      </p>
      <div
        className="surface-sunken h-1.5 w-full max-w-sm overflow-hidden rounded-full"
        role="progressbar"
        aria-valuenow={Math.round(progress.fraction * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Starting the engine"
      >
        <div
          className="h-full rounded-full bg-emerald-500 transition-[width] duration-300"
          style={{ width: `${Math.max(progress.fraction * 100, 2)}%` }}
        />
      </div>
      <p className="text-muted text-[11px]">{progress.message}</p>
    </div>
  );
}

function Failed({ error }: { error: unknown }) {
  /**
   * The escape hatch, and the reason persistence needs one.
   *
   * Databases now survive a refresh, which means a bad one survives too. A
   * visitor whose store cannot be opened would otherwise have a permanently
   * broken page and no way to know it is fixable, worse than a demo that
   * forgets everything. So the failure screen offers the fix rather than
   * describing it.
   */
  const [clearing, setClearing] = useState(false);

  return (
    <div className="flex h-dvh flex-col items-center justify-center gap-3 px-6 font-mono">
      <p className="text-sm text-red-500">The engine did not start.</p>
      <p className="text-muted max-w-lg text-center text-[11px]">{String(error)}</p>
      <button
        type="button"
        disabled={clearing}
        className="rounded border border-[var(--border-subtle)] px-3 py-1.5 text-[11px] hover:bg-[var(--surface-sunken)] disabled:opacity-50"
        onClick={() => {
          setClearing(true);
          void clearStoredData().then(() => window.location.reload());
        }}
      >
        {clearing ? "Clearing…" : "Clear saved databases and reload"}
      </button>
      <p className="text-muted max-w-lg text-center text-[11px]">
        This build needs WebAssembly and about 15 MB of download. Your databases
        are stored in this browser only. Nothing is uploaded anywhere.
      </p>
    </div>
  );
}

export async function boot(root: Root): Promise<void> {
  root.render(
    <StrictMode>
      <Booting progress={{ fraction: 0, message: "Starting…" }} />
    </StrictMode>,
  );

  try {
    const transport = await createWasmTransport((progress) =>
      root.render(
        <StrictMode>
          <Booting progress={progress} />
        </StrictMode>,
      ),
    );
    setTransport(transport);
    // A teaching demo whose engine cannot be poked at from the console is
    // missing the point. `chendb.request("/health")` from devtools reaches the
    // same ASGI app every panel does, and `chendb.clearStoredData()` is the
    // escape hatch for anyone who has wedged their own store.
    Object.assign(window, { chendb: Object.assign(transport, { clearStoredData }) });
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  } catch (error) {
    root.render(
      <StrictMode>
        <Failed error={error} />
      </StrictMode>,
    );
    throw error;
  }
}

export { createRoot };
