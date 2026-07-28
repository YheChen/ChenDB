/**
 * The transport that has no server behind it.
 *
 * Loads CPython compiled to WebAssembly, unpacks the engine's own `.py` files
 * into an in-memory filesystem, and calls the same ASGI app the real server
 * runs — in the tab, with no network in the path.
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
 * works, including the one thing that did not — FastAPI wanting a worker
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
 * base so it can be served from a subdirectory — a GitHub Pages project site
 * lives at `/<repo>/` — and a bare relative specifier in a dynamic import
 * resolves against the importing module instead, which lands in `/assets/`
 * and 404s. Absolute-from-the-document is right in both cases.
 */
const ASSET_BASE = new URL(import.meta.env.BASE_URL || "/", document.baseURI).href;
const ENGINE_SOURCES_URL = `${ASSET_BASE}chendb-engine.json`;
const PYODIDE_INDEX_URL = `${ASSET_BASE}pyodide/`;

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
};

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

  step(0.05, "Fetching a Python interpreter…");
  const pyodide = await (await loadRuntime())({ indexURL: PYODIDE_INDEX_URL });

  step(0.55, "Loading FastAPI and Pydantic…");
  await pyodide.loadPackage("micropip");
  const micropip = pyodide.pyimport("micropip");
  await micropip.install(PACKAGES);

  step(0.8, "Unpacking the engine…");
  const bundle = await fetchEngineBundle();
  writeSources(pyodide, bundle);

  step(0.9, "Opening a database…");
  const bootstrap = (await pyodide.runPythonAsync(
    `${bootstrapSource}\n\nimport types\n_module = types.SimpleNamespace(start=start, handle=handle, subscribe=subscribe, unsubscribe=unsubscribe)\n_module`,
  )) as BootstrapModule;
  const banner = await bootstrap.start();

  step(1, banner);
  return makeTransport(bootstrap);
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
async function loadRuntime(): Promise<typeof LoadPyodide> {
  const module = (await import(
    /* @vite-ignore */ `${PYODIDE_INDEX_URL}pyodide.mjs`
  )) as { loadPyodide: typeof LoadPyodide };
  return module.loadPyodide;
}

async function fetchEngineBundle(): Promise<EngineBundle> {
  const response = await fetch(ENGINE_SOURCES_URL);
  if (!response.ok) {
    throw new Error(
      `the engine bundle is missing (${response.status}). ` +
        `Run \`npm run bundle:engine\` — the WASM build cannot make it up.`,
    );
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

/** The last line with anything on it — a Python traceback's actual exception. */
function lastLine(text: string): string {
  const lines = text.split("\n").filter((line) => line.trim());
  return lines[lines.length - 1] ?? "the engine failed without saying why";
}

function makeTransport(bootstrap: BootstrapModule): Transport {
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
        // needs — `.pop()` alone returns the trailing newline's empty string,
        // and an error message of "" is worse than no error handling at all.
        throw new ApiRequestError(500, "EngineError", lastLine(String(cause)));
      }

      const { status, body: payload } = JSON.parse(raw) as {
        status: number;
        body: unknown;
      };
      if (status === 204) return undefined as T;
      if (status >= 400) throw extractError(status, payload);
      return payload as T;
    },

    subscribe(databaseId, handlers: StreamHandlers) {
      // No connecting, no reconnecting, no closed. The engine is in the same
      // process, so the only honest state is "open" — and the reason the
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
