# Milestone 14: the transport seam

Nothing in this milestone is visible. It is the refactor that makes the next
one possible: **a build of the visualizer that carries the engine with it**, as
CPython compiled to WebAssembly, with no server and no network.

```
  api.getCatalog(id)  ──▶  transport.request("/databases/…/catalog")
                                    │
                         ┌──────────┴──────────┐
                         ▼                     ▼
                   httpTransport         (a WASM transport)
                   fetch + WebSocket     the ASGI app, in the tab
```

---

## The spike that came first

The whole plan turns on one question, so it was answered before any code moved:
**can Pyodide run the real engine and answer an in-process ASGI call?**

Yes.

| | |
|---|---|
| Python | **3.14.2** in WASM, the project requires ≥ 3.13 |
| pydantic | **2.12.5** with `pydantic_core` 2.41.5. The Rust core has a `wasm32` wheel and really validates |
| fastapi | **0.136.1** |
| engine | 90 files, 1 MiB of `.py`, into an in-memory filesystem, **unmodified** |

Running against it, with nothing but MEMFS underneath:

```
GET  /health                    200   milestone 13, 15 features on
CREATE TABLE ×2, INSERT ×2      200   real page writes

SELECT u.name, COUNT(*), SUM(o.total)
  FROM users u JOIN orders o ON u.id = o.user_id
  GROUP BY u.name HAVING SUM(o.total) > 20 ORDER BY spend DESC
  → ada 2 100 · grace 2 100          (alan's 15 correctly cut by HAVING)

EXPLAIN → PhysicalSort / PhysicalProject / PhysicalAggregate / PhysicalHashJoin
```

Page 4 came back as `HEAP`, owner `users`, **`checksum_valid: true`**, LSN
82668, a real CRC32 over real bytes in a real page. The crash button worked
too: `abandon()`, reopen, recovery replays 24 records across 4 finished
transactions, and the committed row survives.

### The one thing that did not work

```
RuntimeError: can't start new thread
  anyio/_backends/_asyncio.py, in run_sync_in_worker_thread
```

FastAPI runs a **synchronous** route handler in a worker thread so a slow one
cannot block the event loop. Every ChenDB router is `def`, not `async def`. WASM
has no threads.

The fix is six lines, and it lives in the browser bootstrap rather than the
engine:

```python
async def _inline(func, *args, **kwargs):
    return func(*args)

anyio.to_thread.run_sync = _inline
starlette.concurrency.run_in_threadpool = ...
```

Safe *there* precisely because there is one tab and one caller, nothing to be
concurrent with. The tempting alternative, making all 25 routers `async def`,
would serialise the real server across databases too, so the browser's problem
would become the server's. Worth remembering if this ever looks like a bug.

### What that bought

Because the ASGI app runs in-process, the browser build reuses the **same
routers, the same mappers, the same Pydantic models and the same error
envelope**. No `engine/server/browser.py`, no duplicated endpoint layer, no
drift guard, and `api.ts` keeps being generated from the one OpenAPI schema,
so Milestone 12's freshness check keeps meaning something.

---

## What actually changed

One function. Every call in the app already funnelled through it:

```ts
async function request<T>(path, init?) {
  response = await fetch(`${API_PREFIX}${path}`, …)   // before
  return getTransport().request<T>(path, init);       // after
}
```

37 call sites, none of them touched. `api.ts` lost 60 lines and became purely
the vocabulary (one method per endpoint) while `transport.ts` took the
plumbing.

Two things moved *out of the app* and into the HTTP transport, because both are
facts about HTTP rather than about the engine:

- **Reconnection.** `useEventStream` owned a WebSocket, an attempt counter and
  exponential backoff. A transport that runs the engine inside the tab cannot
  disconnect, so there would be nothing there for that code to do. The hook
  lost 67 lines and kept the parts that are really its own: bounding memory,
  batching renders, and pausing without dropping the connection.
- **"Cannot reach the engine. Is `python -m engine.server` running?"** An
  in-process transport has no such failure mode and should not have to pretend
  it might.

---

## Proving a seam is a seam

An abstraction with one implementation is a claim, not a fact. So
`transport.test.ts` swaps in a transport with **no network under it at all** and
drives the real `api` object and the real event-stream hook through it, which
is what the WASM build will do, minus a Python interpreter.

Every test in it also asserts this:

```ts
expect(fetchSpy, "the transport was bypassed and fetch was called")
  .not.toHaveBeenCalled();
```

If a later change reaches around `getTransport()` and calls `fetch` directly,
that fails here rather than the WASM build quietly losing an endpoint.

---

## Why the demo is WASM and the ThinkPad is for dev

The deciding fact is not performance. **The API has no authentication**, and no
disk quota:

```
max_open_databases: 16      max_rows_per_query: 10_000
max_page_bytes_returned: 64 KiB      max_executions: 32
```

Those cap responses, not writes. Anyone who can reach the URL can `CREATE TABLE`
and insert until the partition fills. Milestone 1's workspace containment stops
path traversal; it does not stop that. Hosting publicly therefore means building
per-visitor workspaces, a reaper, a quota and a rate limiter *first*, real work
that teaches nothing about databases.

A WASM build makes the whole problem not exist: every visitor gets a private
engine in their own tab. Nothing to isolate, nothing to abuse, and a static file
stays up when a laptop does not.

The real server keeps the things a browser cannot have, genuine `fsync`, the
recovery suite forking real processes and `SIGKILL`ing them, and benchmarks on
hardware that is not shared with a browser. Reached over a tailnet, where the
network *is* the authentication.

---

## A milestone that adds no engine feature

The second one. `MILESTONE_FEATURES` has 12 entries at Milestone 14, and the
check that used to be `==`, then `>= MILESTONE - 1`, would have become `- 2`,
at which point it asserts nothing.

So the exceptions are named instead:

```python
MILESTONES_WITHOUT_ENGINE_FEATURES = frozenset({12, 14})
```

and the assertion is exact again: `len(FEATURES) == MILESTONE - len(exceptions)`.
Skipping one is now a line somebody had to write, with a reason next to it.

---

## Try it

```bash
npm --prefix visualizer test
```

`src/lib/transport.test.ts` is the interesting one.

---

## What is still missing

- **The WASM transport itself.** That is Milestone 15, along with the Pages
  deploy and a CI job so the bundle cannot rot.
- **The event stream under WASM is unproven.** It should become a direct
  `FanoutSink` subscription, which is simpler than a WebSocket, but the spike
  did not test it.
- **Step mode cannot work in a tab**, and this is confirmed rather than
  guessed: `StepController` blocks a thread at each checkpoint. It ships
  disabled behind the existing `/health` features flag, as unbuilt features have
  since Milestone 1.
- **The crash button will need a label.** It genuinely replays the WAL out of
  MEMFS, but `fsync` there has nothing to sync to, so it demonstrates recovery
  from a lost buffer pool, not from a power cut. Saying otherwise would be the
  first thing in this project that overstates itself.
- **~15 MB on first load**: 9.2 MB of WASM, 2.4 MB of stdlib, 2.4 MB of
  wheels, 1 MB of engine. Maybe 6–8 MB compressed, cached afterwards.
- **The spike ran under Node, not a browser.** Threads and MEMFS behave the
  same; load time and memory are unmeasured.
