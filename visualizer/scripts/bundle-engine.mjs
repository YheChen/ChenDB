/**
 * Put the engine and a Python interpreter where the browser can fetch them.
 *
 *     node scripts/bundle-engine.mjs
 *
 * Writes two things into `wasm-public/`, both generated and both gitignored:
 *
 *     chendb-engine.json    every engine/*.py, as {path: source}
 *     pyodide/              the Pyodide runtime, copied from node_modules
 *
 * Its own directory rather than `public/`, because Vite copies `publicDir`
 * into *every* build and the HTTP build has no use for 15 MB of interpreter.
 * `vite.config.ts` swaps the two on `VITE_CHENDB_WASM`.
 *
 * **The engine is not vendored, copied or rewritten** — this reads the same
 * `engine/` the tests import, so the WASM build cannot drift from the real
 * one. If it is stale, it is stale by exactly one `npm run bundle:engine`.
 *
 * JSON rather than a zip or a wheel: Node can write it with no dependencies,
 * a CDN gzips text as well as any archive would, and there is no unpack step
 * to fail in a way nobody can debug from a browser console.
 *
 * Pyodide is copied out of `node_modules` rather than loaded from a CDN, and so
 * are the wheels it needs — resolved as a dependency closure out of
 * `pyodide-lock.json`, which is the same file Pyodide itself resolves against.
 * Anything not already cached in `node_modules` is fetched once, here, at build
 * time. The result is a demo that does not go down when somebody else's CDN
 * does, and a wheel set that cannot drift from the interpreter it was built
 * for.
 */

import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const VISUALIZER = resolve(HERE, "..");
const REPO = resolve(VISUALIZER, "..");
const OUT = join(VISUALIZER, "wasm-public");

/** Pyodide ships far more than a browser needs; these are the parts it does. */
const PYODIDE_FILES = [
  "pyodide.mjs",
  "pyodide.asm.mjs",
  "pyodide.asm.wasm",
  "python_stdlib.zip",
  "pyodide-lock.json",
];

/** What `wasmBootstrap.py` imports beyond the standard library. */
const PYTHON_PACKAGES = ["micropip", "fastapi", "pydantic"];

/** Where a wheel comes from when it is not already cached in node_modules. */
const PYODIDE_CDN = (version, file) =>
  `https://cdn.jsdelivr.net/pyodide/v${version}/full/${file}`;

function collectPython(root) {
  const files = {};
  let bytes = 0;
  const walk = (dir) => {
    for (const name of readdirSync(dir).sort()) {
      if (name === "__pycache__") continue;
      const full = join(dir, name);
      if (statSync(full).isDirectory()) {
        walk(full);
        continue;
      }
      if (!name.endsWith(".py")) continue;
      const source = readFileSync(full, "utf8");
      files[relative(REPO, full)] = source;
      bytes += Buffer.byteLength(source);
    }
  };
  walk(root);
  return { files, bytes };
}

function version() {
  const init = readFileSync(join(REPO, "engine", "__init__.py"), "utf8");
  return init.match(/^__version__ = "([^"]+)"/m)?.[1] ?? "unknown";
}

/**
 * Every wheel needed to import `PYTHON_PACKAGES`, transitively.
 *
 * Walked out of the lock file rather than listed by hand, because the list is
 * eighteen entries long, half of them are transitive, and a hand-written one
 * would be wrong the first time a dependency changed. Names in `depends` use
 * hyphens where the module uses underscores — `pydantic_core` is
 * `pydantic-core` here — so both spellings are tried.
 */
function resolveWheels(lock) {
  const wanted = new Set();
  const visit = (name) => {
    const key = lock.packages[name] ? name : name.replace(/_/g, "-");
    const entry = lock.packages[key];
    if (!entry || wanted.has(key)) return;
    wanted.add(key);
    for (const dependency of entry.depends ?? []) visit(dependency);
  };
  for (const name of PYTHON_PACKAGES) visit(name);
  return [...wanted].map((name) => lock.packages[name].file_name).sort();
}

async function copyWheels(source, target, lock, pyodideVersion) {
  const wheels = resolveWheels(lock);
  let bytes = 0;
  let fetched = 0;
  for (const file of wheels) {
    const cached = join(source, file);
    const destination = join(target, file);
    if (existsSync(cached)) {
      cpSync(cached, destination);
    } else {
      const url = PYODIDE_CDN(pyodideVersion, file);
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`cannot fetch ${file} (${response.status}) from ${url}`);
      }
      writeFileSync(destination, Buffer.from(await response.arrayBuffer()));
      fetched += 1;
    }
    bytes += statSync(destination).size;
  }
  return { count: wheels.length, fetched, bytes };
}

function copyPyodide() {
  const source = join(VISUALIZER, "node_modules", "pyodide");
  if (!existsSync(source)) {
    throw new Error("pyodide is not installed; run `npm install` first");
  }
  const target = join(OUT, "pyodide");
  rmSync(target, { recursive: true, force: true });
  mkdirSync(target, { recursive: true });

  let bytes = 0;
  for (const name of PYODIDE_FILES) {
    const from = join(source, name);
    if (!existsSync(from)) {
      throw new Error(`pyodide is missing ${name}; the package layout changed`);
    }
    cpSync(from, join(target, name));
    bytes += statSync(from).size;
  }
  return bytes;
}

const engine = collectPython(join(REPO, "engine"));
mkdirSync(OUT, { recursive: true });
writeFileSync(
  join(OUT, "chendb-engine.json"),
  JSON.stringify({ version: version(), files: engine.files }),
);

const runtime = copyPyodide();

const pyodideDir = join(VISUALIZER, "node_modules", "pyodide");
const lock = JSON.parse(readFileSync(join(pyodideDir, "pyodide-lock.json"), "utf8"));
const pyodideVersion = JSON.parse(
  readFileSync(join(pyodideDir, "package.json"), "utf8"),
).version;
const wheels = await copyWheels(
  pyodideDir,
  join(OUT, "pyodide"),
  lock,
  pyodideVersion,
);

const mb = (n) => `${(n / 1048576).toFixed(1)} MB`;
console.log(
  `engine ${version()}: ${Object.keys(engine.files).length} files, ${mb(engine.bytes)}\n` +
    `pyodide ${pyodideVersion} runtime: ${mb(runtime)}\n` +
    `wheels: ${wheels.count} (${wheels.fetched} downloaded), ${mb(wheels.bytes)}\n` +
    `-> ${relative(REPO, OUT)}/`,
);
