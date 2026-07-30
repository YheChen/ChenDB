# Milestone 16: deployment, and a database that survives a refresh

The browser build shipped in Milestone 15 and forgot everything when the tab
closed. That made it a toy: open the link, create a schema, insert some rows,
refresh, and it is gone.

Now `/workspace` is backed by IndexedDB. The same file, the same bytes, the
same checksums, kept in the browser instead of in memory.

```
  Milestone 15                    Milestone 16
  ────────────                    ────────────
  browser                         browser
  └── MEMFS                       └── IDBFS ──▶ IndexedDB "/workspace"
      └── shop.chendb                 └── shop.chendb
          (gone on close)                 (still there next week)
```

---

## The mechanism, and the one line that matters

Emscripten's `IDBFS` is the same filesystem interface `MEMFS` is, so **nothing
in the engine changed.** `open()`, `read()`, `write()` and `zlib.crc32` are
untouched; the pager does not know where its bytes end up.

Two calls do the work:

```ts
FS.mount(FS.filesystems.IDBFS, {}, "/workspace");
await syncfs(true);    // populate memory FROM the store, at boot
await syncfs(false);   // write memory TO the store, after a change
```

The line that matters is the one before every `syncfs(false)`:

```python
_app.state.workspace.close_all()
```

Persisting stores the **filesystem**, and a page still sitting in the buffer
pool is not on the filesystem yet. Syncing without closing first would store
whatever happened to have been written through, which is precisely the state
recovery exists to repair, and not what somebody who typed a statement and
closed the tab is entitled to. A test asserts the ordering, because getting it
backwards would produce a store that is *usually* fine.

---

## When it syncs

Not after every request. A `syncfs` per `INSERT` makes a twenty-row demo twenty
IndexedDB transactions.

Not on a long timer either. The gap between the last write and the flush is
data a visitor can lose by closing the tab, so it is **400 ms**, plus an
immediate flush on `pagehide` and on the tab becoming hidden.

`pagehide` is used rather than `beforeunload` because the latter does not fire
reliably when a tab is closed on mobile or frozen by the browser. Neither can
`await`, so **the debounce narrows the window rather than closing it**. A write
in the last 400 ms before a hard close can still be lost. That is a real limit,
not a rounding error, and it is why the interval is short.

Anything that is not a `GET` schedules a sync. Being generous is the safe
direction: an extra sync after a `SELECT` costs a few milliseconds, a missed one
after an `INSERT` costs the visitor's work.

---

## Persistence needs an escape hatch

A database that survives a refresh means **a broken one survives too.** Without
a way out, a visitor whose store cannot be opened has a permanently broken page
and no way to know it is fixable, strictly worse than a demo that forgets.

Two things address it.

**A format-version stamp.** `/workspace/.chendb-format` holds the engine's
`FORMAT_VERSION`, and boot compares it before opening anything. A mismatch
clears the store and says so. The *format* version, not the engine version:
1.5.0 to 1.6.0 need not change a single byte on disk, and clearing somebody's
databases because the UI changed would be gratuitous. A test asserts the browser
build imports that constant from the engine rather than copying it, because a
copy is the thing that silently stops matching.

**A button on the failure screen.** "Clear saved databases and reload", right
under whatever went wrong, plus `chendb.clearStoredData()` from the console.

`clearStoredData` has one rule, and it is in the docstring in bold: **reload
immediately after.** It deletes the IndexedDB database that IDBFS is currently
mounted over, which leaves the mount pointing at nothing. The in-memory files
are still there so the session looks fine, and the next sync writes into a store
that has been recreated underneath it. Every caller in the app reloads.

---

## Two mistakes worth recording

**I swallowed the error.** The first version ended `persist()` with
`.catch(() => {})`. The explicit sync worked and the debounced one did not, and
because the failure was silent, "nothing persisted" and "persisting threw" were
indistinguishable from outside. It cost three rounds of debugging to notice, and
the fix (log it, count the failures) is now the thing that would have made it
one. Losing a visitor's work quietly is the only outcome worse than losing it
loudly.

**I tested it wrong.** Then I called `clearStoredData()` and carried on in the
same session, which is exactly the unsupported state described above, and
concluded persistence was broken when it was not. The rule went into the
docstring as a direct result.

---

## Verified in a real browser

Create, hard reload, and read it back:

```
before reload   shop: users 3 rows, orders 4 rows
── reload ──
after reload    databases: [shop]      tables: orders:4, users:3
                SELECT u.city, COUNT(*), SUM(o.total) … JOIN … GROUP BY
                  → london 3 190 · ny 1 15
                page 4: HEAP · owner users · checksum_valid TRUE
                INSERT one more row → 4
── reload ──
after reload    id/city: 1 london, 2 ny, 3 london, 4 berlin
                crash → recovered 2 record(s); 2 finished, 0 interrupted
                rows after crash: 4
```

The strongest evidence is `checksum_valid: true`. A CRC32 computed before the
page went into IndexedDB still validates coming back out, which means the round
trip is byte-exact rather than approximately right. And `berlin` (written
*after* the first restore) survived the second reload, so the cycle works
repeatedly rather than only once.

The crash button still recovers on a database restored from IndexedDB.

---

## Where it is deployed

Vercel, from `vercel.json` at the repo root, with versioned asset paths so 12 MB
of interpreter can be cached `immutable` honestly. GitHub Pages was the first
attempt and is not used: it cannot set response headers at all, so every load
would revalidate the lot.

---

## What is still missing

- **`durable_fsync` is still false, and still says so.** `fsync` does not reach
  IndexedDB; the sync is a separate explicit step. The crash button demonstrates
  recovery from a lost buffer pool, not from a power cut, and the WAL panel is
  unchanged in saying that.
- **A write in the last 400 ms before a hard close can be lost**, as above.
- **No quota handling.** IndexedDB can refuse a write when the origin is over
  budget. The failure is now logged rather than swallowed, but nothing tells the
  visitor to delete a database, and nothing measures how much room is left.
- **Nothing is shared.** The store is per-origin and per-browser, so a link
  cannot carry a database to somebody else. Exporting a `.chendb` file would be
  the obvious next step, and the engine already has the bytes.
- **No migration.** A format bump clears the store rather than upgrading it,
  which is the honest choice for a demo and would not be for a product.
