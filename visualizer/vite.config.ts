import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * The visualizer is a pure client of the engine's HTTP + WebSocket API.
 * In development it proxies /api to the Python server so the browser sees a
 * single origin, which keeps cookies, CORS and WebSocket upgrades simple.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
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
