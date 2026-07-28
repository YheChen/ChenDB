/**
 * One entry point, two builds.
 *
 * `VITE_CHENDB_WASM` is set by `npm run build:wasm` and by nothing else, so
 * the development build takes the same path it always has: mount immediately
 * and talk to `python -m engine.server` over HTTP. The WASM build has to start
 * an interpreter first, which is the only reason this is not three lines.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

const root = createRoot(container);

if (import.meta.env.VITE_CHENDB_WASM) {
  // Imported lazily so a development bundle never carries any of it, and
  // with `.then` rather than top-level await, which needs a build target
  // several years newer than the browsers this should open in.
  void import("./wasmBoot").then(({ boot }) => boot(root));
} else {
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
