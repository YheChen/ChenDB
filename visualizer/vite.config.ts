import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * Two builds from one source tree.
 *
 * The default one is a pure client of the engine's HTTP + WebSocket API, and
 * in development it proxies /api to the Python server so the browser sees a
 * single origin, which keeps cookies, CORS and WebSocket upgrades simple.
 *
 * `VITE_CHENDB_WASM=1` builds the one that carries the engine with it. The
 * only difference here is where static assets come from: `wasm-public/` holds
 * the Pyodide runtime and the bundled engine sources, and Vite copies
 * `publicDir` into every build, so pointing the HTTP build at it would put
 * 15 MB of interpreter into a bundle that will never load it.
 */
const wasm = Boolean(process.env.VITE_CHENDB_WASM);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  publicDir: wasm ? "wasm-public" : "public",
  resolve: {
    // Must mirror `paths` in tsconfig.json: tsc resolves the alias when
    // typechecking, and Rollup needs it again when bundling.
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
