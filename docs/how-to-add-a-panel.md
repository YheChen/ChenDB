# Adding a visualizer panel

The engine side is covered by `docs/how-to-instrument.md`. This is the rest.

## 1. Expose it over HTTP

**Schema** — a Pydantic model in `engine/server/schemas/`:

```python
class BTreeNodeModel(ApiModel):
    page_id: int
    level: int
    keys: list[str]
    children: list[int]
    next_leaf_id: int | None
```

**Mapper** — in `engine/server/mappers.py`, and nowhere else:

```python
def btree_node_to_api(node: BTreeNode) -> BTreeNodeModel:
    return BTreeNodeModel(...)
```

**Router** — a synchronous handler, so FastAPI runs it in a worker thread:

```python
@router.get("/indexes/{index_name}", response_model=BTreeModel)
def get_index(managed: DatabaseDep, index_name: str) -> BTreeModel:
    with managed.use() as db:              # lock held
        snapshot = db.index_snapshot(index_name)   # immutable copy
    return mappers.btree_to_api(snapshot)  # lock released: pure CPU
```

Take the lock, copy a snapshot, release, then map. Never serialize under the
lock — one slow client would stall every query.

**Feature flag** — flip it in `engine/server/app.py`:

```python
FEATURES = { ..., "indexes": True }
```

## 2. Regenerate the types

```bash
python scripts/generate_api_types.py
```

Writes `docs/openapi.json` and `visualizer/src/types/api.ts`. Never hand-edit
the latter — a renamed Pydantic field should break the TypeScript build, and it
only can if the file is generated.

## 3. Add the client call and hook

`visualizer/src/lib/api.ts`:

```ts
getIndex: (id: string, name: string) =>
  request<BTreeModel>(`/databases/${id}/indexes/${name}`),
```

`visualizer/src/hooks/useEngine.ts` — and add the key to
`invalidateDatabase()` if a write changes it:

```ts
export function useIndex(id: string | null, name: string | null) {
  return useQuery({
    queryKey: queryKeys.index(id ?? "", name ?? ""),
    queryFn: () => api.getIndex(id!, name!),
    enabled: Boolean(id && name),
  });
}
```

## 4. Build the panel

One directory under `visualizer/src/features/`. Use `Panel` from
`components/primitives` so the chrome matches, and handle all four states —
loading, error, empty, and populated. An empty state should say what to do
next, not just "no data".

```tsx
export function IndexPanel({ databaseId, indexName }: Props) {
  const query = useIndex(databaseId, indexName);
  return (
    <Panel title="B+ tree" subtitle={indexName ?? undefined} className="h-full">
      {!indexName ? <EmptyState title="No index selected" hint="…" />
        : query.isPending ? <Spinner label="Reading index" />
        : query.isError ? <ErrorNotice error={query.error} onRetry={query.refetch} />
        : <TreeView tree={query.data} />}
    </Panel>
  );
}
```

## 5. Mount it behind the flag

In `visualizer/src/pages/ExplorerPage.tsx`:

```tsx
const health = useHealth();
...
{health.data?.features.indexes ? <IndexPanel … /> : null}
```

Hidden, not disabled. A greyed-out control for something that does not exist is
worse than no control.

## 6. Test it

`visualizer/src/features/index/IndexPanel.test.tsx` — stub `fetch`, render, and
assert on what a user can perceive: roles, accessible names, visible text.
Every interactive element needs an accessible name; a row whose content is
entirely visual (a badge, a bar, a number) needs an explicit `aria-label`.

```tsx
expect(
  await screen.findByRole("button", { name: "Inspect page 2, HEAP, owned by users" }),
).toBeInTheDocument();
```

## Conventions

- **Dense, monospace, tabular figures.** Numbers line up in columns.
- **Show cost.** Rows scanned, pages read, elapsed. Cost visible is the point
  of the tool.
- **Colour means something.** Page type, slot state, event category. Never
  decoration. `toneForPageType()` keeps it consistent across panels.
- **Both themes.** Use the CSS variables in `index.css`, not hard-coded colours.
- **Keyboard reachable.** Focus is visible everywhere; the split dividers
  respond to arrows, Home and End.
- **Bounded rendering.** Cap what goes into the DOM (`HexView` renders at most
  8 KiB) and say so when truncating.

## If the panel has a "try it" button

Any SQL the panel will run on the user's behalf goes in
`visualizer/src/lib/demoSql.ts`, not inline in the component.

```ts
export const WORKLOADS: Workload[] = [
  { id: "scan", label: "Scan once", hint: "…", sql: (table) => `SELECT * FROM ${table};` },
];
```

Two reasons, and both are bugs this project has already shipped:

- **Build it from the open table, never from a guessed column name.**
  `demoRows.ts` has `insertRow`, `literalFor` and `notNullColumn` for exactly
  this. A hardcoded `VALUES (1, 'x')` works until someone's table has three
  columns, and then the button reports `row has 2 values but 3 columns` —
  the demonstration failing for a reason that has nothing to do with what it
  was demonstrating.
- **The catalogue is enumerable, so it gets tested.**
  `tests/integration/test_demo_sql.py` asks Node to evaluate that module and
  runs every statement in it against a real engine. A button that produces SQL
  the parser refuses fails CI by name. One shipped and sat in the UI for a
  whole milestone before that existed.

State what the statement should do — `parses`, `runs: "ok" | "error" | "skip"`,
and which fixture it needs. A demo that is *supposed* to fail is as much a
claim as one that works.

Check it with:

```bash
make demo-sql
```
