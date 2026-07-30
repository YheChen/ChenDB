# Milestone 12: CI, and the guard for what slipped

Eleven milestones with no `.github/` directory. The cost of that is not
hypothetical; it is two specific bugs that this project already shipped:

| | |
|---|---|
| Milestone 1–11 | **`make lint` was red the whole time.** It runs `ruff check` *and* `ruff format --check`, and running the first by hand looks like linting. Nobody ran the target. |
| Milestone 10 | **A demo button shipped SQL the parser refused.** `DELETE FROM … WHERE id = 1`, in a workspace where `DELETE` was not implemented. It sat there for a milestone. |

Both have the same shape: a check that exists and is not run. Neither is a hard
problem. The format check is one command, and the demo button was four lines
from being tested. What was missing is anything that runs them when a human
does not.

---

## What was built

```
.github/workflows/ci.yml            three jobs, on every push and PR
visualizer/src/lib/demoSql.ts       every demo statement, in one place
scripts/emit_demo_sql.ts            prints that catalogue as JSON
tests/integration/test_demo_sql.py  runs every one against a real engine
Makefile                            `make ci`, `make examples`, `make demo-sql`
```

---

## Three jobs, split by what they need

```
  engine     bare python, nothing installed   → import + every example
  python     pip install -e '.[server,dev]'   → ruff, ruff format, pytest, types
  frontend   npm ci                           → tsc, vitest, vite build
```

Split by *dependency*, not by category, so a failure names its own cause. The
first job is the interesting one: it installs **nothing**, because the engine's
central claim is that it depends on nothing but the standard library, and the
only honest way to check that is not to install anything. `pyproject.toml`
declares `dependencies = []` and `test_architecture_boundaries.py` greps the
imports, but neither would catch a stray `import httpx` behind a `try`. Running
on a bare interpreter would.

Three checks are worth naming because each one closes a specific way this
project has drifted:

- **`ruff format --check` runs as its own step**, next to `ruff check`, so the
  two are visibly different things.
- **The committed TypeScript is regenerated and diffed.** `visualizer/src/types/api.ts`
  is generated from the live Pydantic models and committed so the frontend
  builds without a server. That is only safe if something notices when it goes
  stale.
- **`vite build` runs, not just `tsc --noEmit`.** A typecheck will not catch an
  import path that Vite resolves differently, and a visualizer that typechecks
  but does not bundle is still broken.

---

## The demo-SQL guard

Every "try it" button in the explorer writes real SQL against the user's real
tables. There were about twenty-five such statements, scattered across six
components, and nothing had ever run one.

They now live in one module, `visualizer/src/lib/demoSql.ts`, and each carries
what it claims about itself:

```ts
{
  id: "mvcc/versions/bob",
  sql: "UPDATE users SET email = 'demo-7777';",
  parses: true,        // the parser must accept it
  runs: "ok",          // a button runs it, and it must succeed
  on: "seeded",        // against the fixture table, with rows
}
```

`runs` has three values, and the third is the one that makes the guard honest:

- `"ok"`: a button runs it and it must succeed.
- `"error"`: a button runs it and it is **meant** to fail. "Break it half-way"
  has no point if the last row succeeds, so "it errored" is the assertion.
- `"skip"`: never run by a button. The editor's examples illustrate syntax
  against whatever tables the user happens to have, and two of them are
  deliberately invalid. `parses` is still checked for all of them, so
  "not implemented yet" going quietly out of date is caught too.

### Reading TypeScript from a Python test

The catalogue has to live in the frontend, because that is where the app reads
it from. A copy in the test suite is the failure mode this is meant to end,
not repeat. So the test asks Node to evaluate the module and print JSON:

```python
subprocess.run(["node", "scripts/emit_demo_sql.ts"], ...)
```

Node 22.6+ strips TypeScript types natively, so this needs no bundler, no
transpiler and **no new dependency**, which matters, because a guard that is
expensive to run is one that gets turned off. Two consequences worth knowing:

- Type-only imports are erased without being resolved, so `import type { … }
  from "@/types/api"` works under Node despite the alias.
- *Value* imports are not, so `demoSql.ts` imports `./demoRows.ts` with the
  extension spelled out, and `allowImportingTsExtensions` is on.

Nothing is generated to disk and nothing is committed. A generated fixture
would be one more thing that can be stale, which is precisely the bug class
this guard exists for.

If `node` is missing the guard skips, except in CI, where `CHENDB_REQUIRE_NODE`
turns the skip into a failure. **A guard that goes quiet is worse than no
guard**, because it looks like a pass.

### The fixture is chosen to be awkward

```
users(id INTEGER PRIMARY KEY, email TEXT NOT NULL, age INTEGER, active BOOLEAN)
```

Four columns, a `NOT NULL` that is not the key, and a `BOOLEAN`. That shape is
deliberate: any statement written by hand rather than built from
`demoRows.ts` would almost certainly assume two or three columns and fail
immediately. A two-column fixture would let an arity bug through, so a separate
test asserts the fixture still has the properties the others rely on, otherwise
the guard could pass by being easy.

### It was checked against the bugs it exists for

Both historical failures were reintroduced and the guard caught both, naming
the button:

```
buffer/scan (Scan once) was refused: DROP is not implemented yet
mvcc/invisible/bob (A reader does not wait) is a button the user can press
    and it fails: assert 422 == 200
```

A guard nobody has watched fail is a guard nobody knows works.

---

## What this did *not* find

Worth being clear about, because a green tick is a claim.

CI would not have caught the four bugs Milestone 11 turned up. The write-write
conflict, the never-committed session transaction, the quadratic WAL, the
catalog counting versions as rows. Every one of them needed a *test that did
not exist*, and automation only runs the tests you have. What CI catches is
**regression and neglect**: a check that used to pass and does not, a generated
file that drifted, a demo that stopped matching the engine.

The two bugs in the table at the top are exactly that class, which is why they
are the ones it is built around.

---

## A milestone that adds no engine feature

`MILESTONE_FEATURES` had one entry per milestone for eleven milestones, and the
CLI banner prints them joined with `+`. This milestone adds nothing to that
list: CI is a guarantee about the other eleven, not a twelfth thing the engine
can do, and `storage + SQL + execution + … + CI` is a category error.

So the invariant moved from `len(MILESTONE_FEATURES) == MILESTONE` to
`MILESTONE - 1 <= len(…) <= MILESTONE`. The list may lag; it may never lead,
which would mean a feature was announced before it existed.

---

## Try it

```bash
make ci
```

Runs lint, typecheck, both test suites and every example, in CI's order.

```bash
make demo-sql
```

Prints every statement the explorer's buttons will produce.

---

## What is still missing

- **No coverage gate.** `make coverage` exists and nothing enforces a number.
  A threshold invites tests written to move it.
- **No frontend formatter.** The Python side has `ruff format --check`; the
  TypeScript has nothing equivalent wired up.
- **No matrix.** One Python and one Node version. The engine claims 3.13+ and
  only 3.13 is tested.
- **No caching of the venv**, only of pip's downloads, so the Python job
  reinstalls every run. A few seconds, and not worth the staleness risk yet.
- **Nothing runs the recovery tests under load or repetition.** They fork real
  processes and `SIGKILL` them; a flake there would be a real bug and would
  currently look like noise.
