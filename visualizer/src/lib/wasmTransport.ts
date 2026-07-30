/**
 * The transport that has no server behind it.
 *
 * Loads CPython compiled to WebAssembly, unpacks the engine's own `.py` files
 * into an in-memory filesystem, and calls the same ASGI app the real server
 * runs, in the tab, with no network in the path.
 *
 *     transport.request("/databases/demo/query")
 *         │
 *         ▼
 *     bootstrap.handle("POST", "/api/v1/databases/demo/query", body)
 *         │
 *         ▼  httpx.ASGITransport
 *     the same routers, mappers and Pydantic models as the HTTP build
 *
 * Nothing is reimplemented here, which is the whole point: an endpoint added
 * to the server is available to this build the moment the engine sources are
 * rebundled, and `api.ts` stays generated from one OpenAPI schema.
 *
 * See `docs/milestone-14-transport.md` for the spike that established this
 * works, including the one thing that did not, FastAPI wanting a worker
 * thread, which `wasmBootstrap.py` patches out.
 */

import type { PyodideInterface, loadPyodide as LoadPyodide } from "pyodide";
import bootstrapSource from "./wasmBootstrap.py?raw";
import {
  API_PREFIX,
  ApiRequestError,
  extractError,
  type StreamHandlers,
  type Transport,
} from "./transport";

/** Everything the engine needs beyond the standard library. */
const PACKAGES = ["fastapi", "pydantic"];

/**
 * Where the bundled sources and the Pyodide runtime are served from.
 *
 * Resolved against the *document*, not this module. The build uses a relative
 * base so it can be served from a subdirectory, a GitHub Pages project site
 * lives at `/<repo>/`, and a bare relative specifier in a dynamic import
 * resolves against the importing module instead, which lands in `/assets/`
 * and 404s. Absolute-from-the-document is right in both cases.
 */
const ASSET_BASE = new URL(import.meta.env.BASE_URL || "/", document.baseURI).href;

/**
 * Names the two big assets, which carry their versions in their paths.
 *
 * One small uncached request buys `immutable` on the other twelve megabytes.
 * Without it the interpreter and the engine bundle would keep fixed names
 * across releases, and a browser holding yesterday's `pyodide.asm.wasm` or
 * `chendb-engine.json` would run the wrong one with no visible symptom.
 */
const MANIFEST_URL = `${ASSET_BASE}wasm-manifest.json`;

export type BootProgress = {
  /** 0 to 1, for a progress bar that does not lie about being indeterminate. */
  fraction: number;
  message: string;
};

type EngineBundle = {
  version: string;
  files: Record<string, string>;
};

type BootstrapModule = {
  start: () => Promise<string>;
  handle: (method: string, path: string, body: string | null) => Promise<string>;
  subscribe: (databaseId: string, emit: (record: string) => void) => void;
  unsubscribe: (databaseId: string) => void;
  /** The on-disk format version this build writes. */
  formatVersion: () => string;
  /** What the persisted store was written by, or "" if it is empty. */
  storedVersion: () => string;
  /** Flush every open handle, so the filesystem is current before a sync. */
  close: () => void;
};

/**
 * Where IndexedDB backs the filesystem, and the name of that database.
 *
 * Emscripten's IDBFS names its IndexedDB store after the mount point, so this
 * one constant is both the path Python writes to and the thing
 * `clearStoredData` deletes.
 */
const WORKSPACE = "/workspace";

/** How long a burst of writes is allowed to coalesce before it is stored. */
const PERSIST_DEBOUNCE_MS = 400;

/**
 * Start the engine in this tab.
 *
 * Takes a few seconds and about 15 MB the first time, so `onProgress` reports
 * each stage by name rather than leaving a spinner to imply something is
 * broken. The stages are honest about their relative cost: fetching the
 * interpreter dominates, and pretending otherwise makes the bar stall at 90%.
 */
export async function createWasmTransport(
  onProgress: (progress: BootProgress) => void = () => {},
): Promise<Transport> {
  const step = (fraction: number, message: string) =>
    onProgress({ fraction, message });

  const manifest = await fetchManifest();
  const pyodideIndexUrl = `${ASSET_BASE}${manifest.pyodide}`;

  step(0.05, "Fetching a Python interpreter…");
  const pyodide = await (await loadRuntime(pyodideIndexUrl))({
    indexURL: pyodideIndexUrl,
  });

  step(0.55, "Loading FastAPI and Pydantic…");
  await pyodide.loadPackage("micropip");
  const micropip = pyodide.pyimport("micropip");
  await micropip.install(PACKAGES);

  step(0.75, "Looking for anything you saved…");
  await mountPersistent(pyodide);

  step(0.8, "Unpacking the engine…");
  const bundle = await fetchEngineBundle(`${ASSET_BASE}${manifest.engine}`);
  writeSources(pyodide, bundle);

  step(0.9, "Opening a database…");
  const bootstrap = (await pyodide.runPythonAsync(
    [
      bootstrapSource,
      "import types",
      // camelCase on the JS side, snake_case in the Python: the boundary is the
      // right place for that translation, not either language's own file.
      "_module = types.SimpleNamespace(",
      "    start=start, handle=handle, subscribe=subscribe, unsubscribe=unsubscribe,",
      "    formatVersion=format_version, storedVersion=stored_version, close=close,",
      ")",
      "_module",
    ].join("\n"),
  )) as BootstrapModule;

  // Before the app opens anything, decide whether what is stored is even
  // readable by this build. A database is a binary file with a version in its
  // meta page, so a format bump makes yesterday's bytes unopenable, and an
  // unopenable database in IndexedDB is a demo that is permanently broken for
  // that visitor, with nothing on screen to explain why.
  const stored = bootstrap.storedVersion();
  const current = bootstrap.formatVersion();
  if (stored && stored !== current) {
    await clearStoredData();
    throw new Error(
      `stored databases were written in format ${stored}, this build reads ` +
        `${current}. They have been cleared. Reload to start fresh.`,
    );
  }

  const banner = await bootstrap.start();
  await persist(pyodide, bootstrap);

  step(1, banner);
  const transport = makeTransport(bootstrap, pyodide);
  // The filesystem, for anyone debugging what did or did not persist.
  Object.assign(transport, { FS: pyodide.FS });
  return transport;
}

/**
 * Back `/workspace` with IndexedDB, and load whatever is already there.
 *
 * Without this the filesystem lives and dies with the tab, which makes the
 * demo a toy: build a schema, refresh, and it is gone. IDBFS is the same
 * Emscripten filesystem the rest of the tree uses, so nothing in the engine
 * knows the difference, `open()` and `write()` are unchanged.
 *
 * `syncfs(true)` populates memory *from* the store, and must happen before the
 * app opens anything.
 */
async function mountPersistent(pyodide: PyodideInterface): Promise<void> {
  const FS = pyodide.FS as unknown as {
    mkdirTree: (path: string) => void;
    mount: (type: unknown, options: object, path: string) => void;
    filesystems: { IDBFS: unknown };
    syncfs: (populate: boolean, callback: (error: unknown) => void) => void;
  };
  FS.mkdirTree(WORKSPACE);
  FS.mount(FS.filesystems.IDBFS, {}, WORKSPACE);
  await new Promise<void>((resolve, reject) => {
    FS.syncfs(true, (error) => (error ? reject(error) : resolve()));
  });
}

/**
 * Copy the filesystem into IndexedDB.
 *
 * `close()` first, because persisting means storing the *filesystem* and a page
 * still in the buffer pool is not on it yet. Storing without that would save
 * whatever happened to be written through, the state recovery exists to
 * repair, and not what someone who typed a statement and closed the tab is
 * entitled to.
 */
async function persist(
  pyodide: PyodideInterface,
  bootstrap: BootstrapModule,
): Promise<void> {
  bootstrap.close();
  const FS = pyodide.FS as unknown as {
    syncfs: (populate: boolean, callback: (error: unknown) => void) => void;
  };
  await new Promise<void>((resolve, reject) => {
    FS.syncfs(false, (error) => (error ? reject(error) : resolve()));
  });
}

/**
 * Delete everything this origin has stored. **Reload immediately after.**
 *
 * A persistent demo that can wedge itself permanently is worse than one that
 * forgets: without an escape hatch, a visitor whose store is unreadable has a
 * broken page forever and no way to know it is fixable. So this is exposed on
 * the transport, offered on the boot-failure screen, and called automatically
 * when the format version has moved.
 *
 * The reload is not politeness, it is required. This deletes the IndexedDB
 * database that IDBFS is *currently mounted over*, which leaves the mount
 * pointing at nothing: the in-memory files are still there, so the session
 * looks fine, and the next sync writes into a store that has been recreated
 * underneath it. Every caller here reloads. Testing this by clearing and
 * carrying on is what made persistence look broken when it was not.
 */
export async function clearStoredData(): Promise<void> {
  await new Promise<void>((resolve) => {
    const request = indexedDB.deleteDatabase(WORKSPACE);
    request.onsuccess = () => resolve();
    request.onerror = () => resolve();
    // A store held open by another tab blocks the delete. Resolving anyway is
    // right: the caller is about to reload, and reporting failure here would
    // stop them doing the one thing that fixes it.
    request.onblocked = () => resolve();
  });
}

/**
 * Pull in Pyodide's loader from the copy we serve, at run time.
 *
 * Deliberately not `import { loadPyodide } from "pyodide"`. That entry point
 * branches on Node versus browser at the top of the file, so a bundler sees
 * `node:fs`, `node:vm` and five others, externalises them with a warning, and
 * emits bare specifiers the browser cannot resolve. The branches are dead in a
 * browser but the imports are not.
 *
 * `@vite-ignore` keeps the URL out of the dependency graph entirely: the file
 * is copied into the build by `bundle-engine.mjs` and fetched like any other
 * asset. Types still come from the package, and are erased.
 */
async function loadRuntime(indexUrl: string): Promise<typeof LoadPyodide> {
  const module = (await import(/* @vite-ignore */ `${indexUrl}pyodide.mjs`)) as {
    loadPyodide: typeof LoadPyodide;
  };
  return module.loadPyodide;
}

type Manifest = { engineVersion: string; engine: string; pyodide: string };

async function fetchManifest(): Promise<Manifest> {
  // `no-cache` revalidates rather than skipping the cache: a 304 on a few dozen
  // bytes is cheap, and it is what makes the assets it names cacheable forever.
  const response = await fetch(MANIFEST_URL, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(
      `no wasm-manifest.json (${response.status}). ` +
        `Run \`npm run bundle:engine\`; the WASM build cannot make it up.`,
    );
  }
  return (await response.json()) as Manifest;
}

async function fetchEngineBundle(url: string): Promise<EngineBundle> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`the engine bundle is missing (${response.status}) at ${url}`);
  }
  return (await response.json()) as EngineBundle;
}

/**
 * Write every `.py` into Pyodide's filesystem under `/app`.
 *
 * A plain JSON object of path to source rather than a zip or a wheel: Node can
 * produce it with no dependencies, the CDN gzips it as well as any archive
 * would, and there is no unpack step that can fail in a way nobody can debug
 * from a browser console.
 */
function writeSources(pyodide: PyodideInterface, bundle: EngineBundle): void {
  const seen = new Set<string>();
  for (const [relative, source] of Object.entries(bundle.files)) {
    const path = `/app/${relative}`;
    const parent = path.slice(0, path.lastIndexOf("/"));
    if (!seen.has(parent)) {
      pyodide.FS.mkdirTree(parent);
      seen.add(parent);
    }
    pyodide.FS.writeFile(path, source);
  }
}

/** The last line with anything on it, a Python traceback's actual exception. */
function lastLine(text: string): string {
  const lines = text.split("\n").filter((line) => line.trim());
  return lines[lines.length - 1] ?? "the engine failed without saying why";
}

function makeTransport(
  bootstrap: BootstrapModule,
  pyodide: PyodideInterface,
): Transport {
  let timer: number | undefined;
  let inFlight: Promise<void> = Promise.resolve();
  let failures = 0;

  /**
   * Store the filesystem soon, coalescing a burst of writes into one sync.
   *
   * Not after every request: a `syncfs` per `INSERT` would make a twenty-row
   * demo twenty IndexedDB transactions. Not on a long timer either, the window
   * between the last write and the flush is data a visitor can lose by closing
   * the tab, so it is a few hundred milliseconds rather than a few seconds.
   */
  /**
   * Persist, and *say so* if it fails.
   *
   * The first version of this swallowed the error, and the debounced sync then
   * failed silently while an explicit one worked, which cost three rounds of
   * debugging to notice, because "nothing persisted" and "persisting threw"
   * look identical from the outside. Losing a visitor's work quietly is the
   * one outcome worse than losing it loudly.
   */
  const run = () => {
    inFlight = persist(pyodide, bootstrap).then(
      () => {
        failures = 0;
      },
      (error) => {
        failures += 1;
        console.error(
          `ChenDB: could not save to this browser (attempt ${failures}).`,
          error,
        );
      },
    );
    return inFlight;
  };

  const schedulePersist = () => {
    if (timer !== undefined) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      timer = undefined;
      void run();
    }, PERSIST_DEBOUNCE_MS);
  };

  const flushNow = () => {
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timer = undefined;
      return run();
    }
    return inFlight;
  };

  // `pagehide` fires when a tab is closed, navigated away from, or frozen on
  // mobile; `beforeunload` does not fire reliably on any of those. Neither can
  // await, so the debounce above is deliberately short. This narrows the window
  // rather than closing it, and that limit is real.
  window.addEventListener("pagehide", () => void flushNow());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") void flushNow();
  });

  return {
    kind: "wasm",

    async request<T>(path: string, init?: RequestInit): Promise<T> {
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? init.body : null;

      let raw: string;
      try {
        raw = await bootstrap.handle(method, `${API_PREFIX}${path}`, body);
      } catch (cause) {
        // A Python-level failure is a bug in the engine, not a bad request,
        // and it must not be dressed up as one. The last *non-empty* line of
        // the traceback is the exception itself, which is what a bug report
        // needs, `.pop()` alone returns the trailing newline's empty string,
        // and an error message of "" is worse than no error handling at all.
        throw new ApiRequestError(500, "EngineError", lastLine(String(cause)));
      }

      const { status, body: payload } = JSON.parse(raw) as {
        status: number;
        body: unknown;
      };
      // Anything that is not a GET may have written. Being generous here is
      // the safe direction: an extra sync after a SELECT costs a few
      // milliseconds, a missed one after an INSERT costs the visitor's work.
      if (method !== "GET" && status < 400) schedulePersist();

      if (status === 204) return undefined as T;
      if (status >= 400) throw extractError(status, payload);
      return payload as T;
    },

    subscribe(databaseId, handlers: StreamHandlers) {
      // No connecting, no reconnecting, no closed. The engine is in the same
      // process, so the only honest state is "open", and the reason the
      // HTTP transport keeps its backoff and this one has none.
      handlers.onState("open");
      bootstrap.subscribe(databaseId, (record) => {
        handlers.onEvents([JSON.parse(record)]);
      });
      return () => {
        handlers.onState("idle");
        bootstrap.unsubscribe(databaseId);
      };
    },
  };
}
