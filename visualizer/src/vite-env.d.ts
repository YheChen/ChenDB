/// <reference types="vite/client" />

// Vite's `?worker` suffix turns a module into a Worker constructor. The
// reference above supplies the declaration; without it, importing Monaco's
// editor worker fails to typecheck.
