# Instrumenting a new engine component

Five steps. The rule the whole design rests on: **the engine emits facts, and
never learns who is listening.**

## 1. Define the event

In `engine/diagnostics/events.py`:

```python
@dataclass(frozen=True, slots=True)
class NodeSplitEvent(DiagnosticEvent):
    """A B+ tree node was split in two."""

    category: ClassVar[EventCategory] = EventCategory.INDEX
    level: ClassVar[TraceLevel] = TraceLevel.STORAGE

    index_name: str
    page_id: int
    new_page_id: int
    level_in_tree: int
    promoted_key: str
    is_root_split: bool
```

Rules:

- **Frozen and flat.** A test can build one and compare it with `==`; the
  generic mapper can serialize it without special cases.
- **`category` and `level` are `ClassVar`s.** They cost no memory per instance
  and allow filtering before an instance exists.
- **No `to_json`, no Pydantic.** The boundary test fails if you add one.
- **Primitive fields.** Anything else is stringified at the boundary, which is
  a fallback, not a plan.
- Export it from `engine/diagnostics/__init__.py`.

Choosing a level: per-operation → `SUMMARY`; per-page → `STORAGE`; per-row or
per-expression → `VERBOSE`. If a full table scan would emit one, it is
`VERBOSE`.

## 2. Accept a tracer

```python
class BPlusTree:
    def __init__(self, pager: Pager, *, tracer: Tracer | None = None) -> None:
        self._pager = pager
        self._tracer = tracer if tracer is not None else NULL_TRACER
```

Never `None`. `NULL_TRACER` has every flag `False`, so call sites need no null
check and the guard below still compiles down to a branch.

## 3. Emit behind the cached flag

```python
if self._tracer.storage:
    self._tracer.emit(NodeSplitEvent(...))
```

`emit` re-checks the level, so the guard is not needed for correctness. It is
needed for speed: Python evaluates arguments before the call, so an unguarded
emit builds the event even at `OFF`. Use `tracer.summary`, `.operator`,
`.storage`, `.verbose`, plain booleans recomputed when the level changes.

Emit *after* the work succeeds, so an event never describes something that did
not happen.

## 4. Prove it changes nothing

Add to `tests/integration/test_tracing.py`'s workload, which already asserts
that the database files produced at `OFF`, `STORAGE` and `VERBOSE` are
byte-identical. If a new event makes that test fail, the instrumentation has a
side effect, find it.

Then assert the event actually fires:

```python
def test_a_split_is_reported(...):
    ...
    splits = [i for i in sink.snapshot() if i.event_type == "NodeSplitEvent"]
    assert splits and splits[0].event.is_root_split is False
```

## 5. Document it

Add a row to the table in `docs/event-schema.md`. That file is the contract the
frontend reads.

## What not to do

- Do not log. `print` and `logging` are not diagnostics; they cannot be
  filtered by level, paginated, or streamed to a browser.
- Do not accumulate state in the engine for the UI's benefit. Emit facts; let
  the consumer aggregate.
- Do not emit inside a tight inner loop without a `VERBOSE` guard.
- Do not hold an engine lock while a sink runs. Sinks are called on the
  emitting thread, `RingBufferSink` is O(1) under its own lock, and
  `CallbackSink` for a WebSocket only does `call_soon_threadsafe`.
