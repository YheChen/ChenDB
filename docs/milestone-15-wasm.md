# Milestone 15 — the whole engine, in a browser tab

No server. No backend. Open a link and a Python interpreter downloads, the
engine's own `.py` files are written into an in-memory filesystem, and the same
ASGI app the real server runs starts answering requests from inside the tab.

```
  before                              after
  ──────                              ─────
  browser                             browser
    │ fetch /api/v1/query               ├── React UI
    ▼                                   ├── CPython, compiled to WebAssembly
  FastAPI ──▶ engine/ ──▶ demo.chendb   │     └── engine/  (unchanged .py)
  (a laptop, port 8000)                 └── an in-memory filesystem
                                              └── demo.chendb
```

The engine source is not vendored, forked or rewritten. `bundle-engine.mjs`
reads the same `engine/` the tests import.

---

## Why a browser and not a server

Not performance, and not novelty. **The API has no authentication and no disk
quota.** `max_rows_per_query` and its neighbours cap *responses*; nothing caps
writes. Anyone who can reach a public URL can `CREATE TABLE` and insert until
the partition fills.

Hosting publicly therefore means building per-visitor workspaces, a reaper, a
quota and a rate limiter *first* — a milestone of load-bearing work that teaches
nothing about databases and, done wrong, is on someone's home network.

In a tab, that entire problem does not exist. Every visitor gets a private
engine, there is nothing to isolate, nothing to abuse, and a static file stays
up when a laptop does not.

---

## What made it possible

The constraint enforced since Milestone 1: **the engine imports nothing but the
standard library.** `struct`, `zlib`, `os`, `time`, `math`, `re`, `threading` —
that is the whole list, and CI checks it every run by importing the engine on a
bare interpreter with nothing installed.

A package with a compiled C extension usually has no wasm build. A stdlib-only
one always works. The rule was never written down for this reason, and it paid
for itself here.

---

## What is actually loaded

| | |
|---|---|
| `pyodide.asm.wasm` | 9.2 MB — CPython 3.14, compiled |
| `python_stdlib.zip` | 2.4 MB |
| 14 wheels | 2.4 MB — FastAPI, Pydantic and their closure |
| `chendb-engine.json` | 1.0 MB — 90 `.py` files |
| the app itself | 2.7 MB |

About 23 MB, roughly 8 MB over the wire compressed, cached afterwards.

Every one of those is **self-hosted**. The wheels are resolved as a dependency
closure out of `pyodide-lock.json` — the same file Pyodide itself resolves
against — and vendored at build time, so the demo does not go down when
somebody else's CDN does, and the wheel set cannot drift from the interpreter
it was compiled for.

---

## Three things that went wrong

Worth recording, because each was invisible until the thing actually ran.

**Vite tried to bundle Pyodide.** Its entry point branches on Node versus
browser at the top of the file, so a bundler sees `node:fs`, `node:vm` and five
others, externalises them with a warning, and emits bare specifiers the browser
cannot resolve. The branches are dead in a browser; the imports are not. Fixed
by not bundling it at all — a `@vite-ignore` dynamic import of the copy we
serve.

**A relative base broke the dynamic import.** `--base ./` is what lets one
artifact work at a project-site subpath and at a root without rebuilding. But a
relative specifier in a dynamic import resolves against the *importing module*,
which lives in `/assets/`, so it 404'd on `/assets/pyodide/pyodide.mjs`.
Resolving against `document.baseURI` is right in both cases.

**JavaScript `null` is not Python `None`.** It arrives as a `JsNull` object,
which fails `is not None` and has no `.encode`. The first request died with
`AttributeError: 'JsNull' object has no attribute 'encode'`. Asking
`isinstance(body, str)` — what the value *is*, rather than what it is not —
works however the caller spells "no body".

And one from Milestone 14's spike, which is why that spike happened first:
FastAPI runs synchronous route handlers on a worker thread, WASM has none, and
`wasmBootstrap.py` patches `anyio.to_thread.run_sync` to run inline.

---

## What is genuinely different, and says so

Two capability flags, false only here. The mechanism is the one every unbuilt
feature has used since Milestone 1: `/health` reports what exists, and the UI
hides the rest instead of letting a button fail.

**`execution_stepping`** — `StepController` pauses an execution on a thread and
resumes it from a later request. A tab has no thread to park it on. The panel
says so:

> Step-through needs a background thread to pause an execution on, and this
> build runs the engine inside the browser tab, which has none. Plans and costs
> below are real; only the stepping is missing.

**`durable_fsync`** — the crash button genuinely works. Abandoning the pager
loses the buffer pool, reopening replays the log, and the rows come back:
`recovered 15 record(s): 0 redone, 13 already current, 0 undone; 2 finished, 0
interrupted`. What an in-memory filesystem cannot demonstrate is a *power cut*,
because there is nothing for `fsync` to reach. The WAL panel says that, and the
`FSYNC 0 µs` counter beside it corroborates it rather than hiding it.

That distinction is the whole reason this milestone has a note in it. A crash
demo that quietly means something weaker than the docs claim would be the first
thing in this project to overstate itself.

---

## Verified in a real browser

Not in Node, not in a test harness:

```
health          1.5.0, milestone 15, execution_stepping false, durable_fsync false
CREATE ×2, INSERT ×2                        real page writes
SELECT u.city, COUNT(*), SUM(o.total)
  FROM users u JOIN orders o ON u.id = o.user_id
  GROUP BY u.city ORDER BY spend DESC       london 3 190 · ny 1 15
page 4          HEAP · owner users · checksum_valid true · 3 live records
crash           15 records recovered, 3 rows before and after
event timeline  103 received, live
```

The event stream has no WebSocket in it. A sink subscribed directly to the
engine's `FanoutSink` hands each record to JavaScript as it is produced — which
is why Milestone 14 moved reconnection and backoff into the HTTP transport,
where they are facts about HTTP rather than about the engine.

---

## Two builds, one source tree

```bash
npm run dev          # talks to `python -m engine.server`, exactly as before
npm run build:wasm   # carries the engine with it
npm run preview:wasm # both, locally
```

`VITE_CHENDB_WASM` selects the entry path and swaps `publicDir`. The WASM
assets live in `wasm-public/` rather than `public/` because Vite copies
`publicDir` into *every* build, and the HTTP build has no use for 15 MB of
interpreter — a mistake made once here, caught by the dev bundle jumping from
2.9 MB to 19 MB.

CI builds both, then checks the WASM one has every asset it needs to boot **and
that `chendb-engine.json` carries the current engine version** — a stale bundle
is the one failure that would look like the engine misbehaving rather than the
build being wrong.

---

## What is still missing

- **Nothing persists.** The filesystem is the tab's; closing it loses the
  database. Fine for a demo, and the reason isolation is free. An IndexedDB
  backing store would fix it and is the obvious next thing.
- **No step mode**, as above. A web worker would give a real thread, at the
  cost of making every call async message-passing across a boundary.
- **A cold load is a few seconds.** Nothing is preloaded and nothing is
  streamed; the progress bar names each stage instead of pretending.
- **No `SharedArrayBuffer`, so no real threads**, which also means a row-lock
  wait in the MVCC workspace freezes the tab until it times out — the same
  outcome the HTTP build has for a different reason, documented in
  `docs/milestone-11-dml.md`.
- **The T14 half is not built.** Real `fsync`, the recovery suite forking and
  `SIGKILL`ing processes, and benchmarks on hardware that is not shared with a
  browser all still want a machine. Over a tailnet, where the network is the
  authentication.
