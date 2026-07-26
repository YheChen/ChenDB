# Diagnostic event schema

## The model

An event is a frozen dataclass carrying only what the emitting component knows.
It has no sequence number, no timestamp, and no idea who will consume it.

```python
@dataclass(frozen=True, slots=True)
class PageReadEvent(DiagnosticEvent):
    category: ClassVar[EventCategory] = EventCategory.STORAGE
    level:    ClassVar[TraceLevel]    = TraceLevel.STORAGE

    page_id: int
    file_offset: int
    source: Literal["disk", "buffer_pool"]
    duration_ns: int
    transaction_id: int | None = None
```

The tracer wraps it in an envelope:

```python
@dataclass(frozen=True, slots=True)
class TraceRecord:
    seq: int            # monotonic per database; also the pagination cursor
    timestamp_ns: int   # time.time_ns() at emission
    category: EventCategory
    level: TraceLevel
    event_type: str     # the event class name
    event: DiagnosticEvent
```

`category`, `level` and `event_type` are denormalised into the envelope so
consumers can filter and render without importing every event class.

Events deliberately have **no** `to_json`, `to_dict` or Pydantic base. Turning
one into an API payload happens only in `engine/server/mappers.py`, and
`tests/unit/test_architecture_boundaries.py` fails if an event grows a
serialization method.

## Trace levels

| Level      | Value | Adds                                                    |
|------------|-------|---------------------------------------------------------|
| `OFF`      | 0     | nothing; every fast-path flag is `False`                |
| `SUMMARY`  | 10    | one event per top-level operation                       |
| `OPERATOR` | 20    | query-plan operator lifecycle (Milestone 3)             |
| `STORAGE`  | 30    | page reads/writes, allocations, record I/O — **default**|
| `VERBOSE`  | 40    | per-row and per-expression detail                       |

Levels are strictly nested and spaced by ten so a tier can be inserted later
without renumbering anything already written to a saved trace. An event is
recorded when its own level is ≤ the tracer's.

`STORAGE` is the visualizer's default: enough to animate the storage engine,
not enough to flood.

## Emitting

Always guard with the cached flag:

```python
if self._tracer.storage:
    self._tracer.emit(PageReadEvent(page_id, offset, "disk", elapsed))
```

`Tracer.emit` re-checks the level, so the guard is never required for
correctness. It is required for *speed*: Python evaluates arguments before the
call, so an unguarded emit constructs the event even at `OFF`. The flags
(`tracer.summary`, `.operator`, `.storage`, `.verbose`) are plain booleans
recomputed when the level changes, making the guard one attribute load and a
branch.

`benchmarks/trace_overhead.py` measures both shapes.

## Sinks

| Sink             | Purpose                                                    |
|------------------|------------------------------------------------------------|
| `NullSink`       | discards; installed when tracing is off                    |
| `RingBufferSink` | bounded history, lock-guarded; `snapshot()` copies atomically |
| `CallbackSink`   | forwards to a function — one per WebSocket connection      |
| `FanoutSink`     | delivers to several; swallows per-sink exceptions          |

`FanoutSink` swallowing exceptions is deliberate. Diagnostics are best-effort:
losing an event is acceptable, losing a query is not.

`RingBufferSink.snapshot()` returns an immutable tuple copied under the sink's
lock, which is what lets an HTTP response describe one consistent instant.

---

## Implemented events

Twenty-three types across six categories. These are the only events that exist
today.

### `lifecycle`

| Event | Level | Fields |
|---|---|---|
| `DatabaseOpenedEvent` | SUMMARY | `database_id`, `page_size`, `page_count`, `created` |
| `DatabaseClosedEvent` | SUMMARY | `database_id`, `page_count`, `pages_written` |

### `storage`

| Event | Level | Fields |
|---|---|---|
| `PageAllocatedEvent` | STORAGE | `page_id`, `page_type`, `recycled` |
| `PageFreedEvent` | STORAGE | `page_id`, `previous_type` |
| `PageReadEvent` | STORAGE | `page_id`, `file_offset`, `source`, `duration_ns`, `transaction_id` |
| `PageWriteEvent` | STORAGE | `page_id`, `file_offset`, `duration_ns`, `transaction_id` |
| `PageCompactedEvent` | STORAGE | `page_id`, `reclaimed_bytes` |
| `FileSyncEvent` | STORAGE | `duration_ns`, `pages_written_since_last_sync` |

`source` is always `"disk"` today; Milestone 7 introduces `"buffer_pool"`.
`transaction_id` is always `None` until Milestone 8. Both fields exist now so
the wire format does not change when those milestones land.

### `record`

| Event | Level | Fields |
|---|---|---|
| `RecordInsertedEvent` | STORAGE | `page_id`, `slot_id`, `length`, `page_free_space_after` |
| `RecordDeletedEvent` | STORAGE | `page_id`, `slot_id` |
| `RecordReadEvent` | VERBOSE | `page_id`, `slot_id`, `length` |
| `HeapScanEvent` | SUMMARY | `action`, `first_page_id`, `pages_scanned`, `rows_emitted`, `duration_ns` |

### `parser` — added in Milestone 2

| Event | Level | Fields |
|---|---|---|
| `TokenizedEvent` | OPERATOR | `source_length`, `token_count`, `duration_ns` |
| `TokenEvent` | VERBOSE | `index`, `token_type`, `lexeme`, `start`, `end`, `keyword` |
| `AstNodeCreatedEvent` | VERBOSE | `node_id`, `node_type`, `start`, `end`, `child_count` |
| `ParsedEvent` | OPERATOR | `statement_count`, `node_count`, `duration_ns` |
| `ParseErrorEvent` | OPERATOR | `message`, `start`, `end`, `line`, `column`, `expected`, `found` |

`AstNodeCreatedEvent` fires as each rule *completes*, so the event order shows
recursive descent building the tree bottom-up: leaves first, root last.
`ParseErrorEvent` is `OPERATOR` rather than `VERBOSE` because a failed parse is a
headline event and the editor needs its position to place a marker.

### `catalog` — added in Milestone 4

| Event | Level | Fields |
|---|---|---|
| `CatalogLookupEvent` | STORAGE | `object_type`, `name`, `found`, `from_cache` |
| `TableCreatedEvent` | SUMMARY | `table_name`, `table_id`, `column_count`, `first_page` |

`from_cache` is the field that matters: a miss costs a full scan of
`chendb_tables` plus one of `chendb_columns`, so the hit rate is what makes the
in-memory catalog cache worth having.

### `operator` — added in Milestone 3

| Event | Level | Fields |
|---|---|---|
| `OperatorEvent` | OPERATOR | `operator_id`, `operator_type`, `action`, `input_rows`, `output_rows`, `row` |
| `ExpressionEvalEvent` | VERBOSE | `operator_id`, `node_id`, `expression`, `result` |
| `QueryExecutedEvent` | SUMMARY | `statement_kind`, `rows_returned`, `rows_affected`, `duration_ns`, `cancelled` |
| `ExecutionStateEvent` | SUMMARY | `execution_id`, `state`, `reason` |

`OperatorEvent.action` is `opened`, `next`, `row_emitted`, `exhausted` or
`closed`. `next` is a call *into* an operator and travels down the tree;
`row_emitted` is a row coming *out* and travels up. Both are needed to show a row
moving through the plan.

`ExpressionEvalEvent` is `VERBOSE` because a filter evaluates its predicate once
per row.

`RecordReadEvent` is `VERBOSE` for the same reason: a scan would otherwise emit
one event per row — exactly the flood trace levels exist to prevent.

---

## `index` — Milestone 5

```
IndexCreatedEvent   SUMMARY  index_name, index_id, table_name, column_name,
                             unique, rows_indexed, root_page      (category: catalog)
IndexSearchEvent    STORAGE  index_name, key, found, matches,
                             pages_visited, depth, duration_ns
IndexDescentEvent   VERBOSE  index_name, page_id, tree_level, child_page_id,
                             separator
NodeSplitEvent      STORAGE  index_name, page_id, new_page_id, tree_level,
                             promoted_key, is_root_split
NodeMergeEvent      STORAGE  index_name, page_id, sibling_page_id, tree_level
RangeScanEvent      STORAGE  index_name, low, high, leaves_visited,
                             rows_emitted, duration_ns
```

`NodeMergeEvent` is declared but **never emitted**: ChenDB does not merge nodes
on delete. It exists so the schema is complete and a consumer can render it if a
later milestone starts merging. Everything else here is emitted by
`engine/index/bplustree.py`.

Two field names are worth explaining. `tree_level` is called that, and not
`level`, because `DiagnosticEvent` already declares `level` as the *trace* level;
re-annotating a `ClassVar` as an instance field makes `dataclass` build a broken
`__init__`. And `key`, `low`, `high` and `promoted_key` arrive **already rendered
as strings** — an encoded key is an order-preserving byte string only
`engine.index.key` can interpret, and carrying the raw bytes plus a column type
would make every consumer of the bus depend on the index package.

`pages_visited` against `depth` is the interesting pair on a search: equal for a
clean descent, larger when duplicates span leaves and the search steps right.

---

## `planner` — Milestone 6

```
StatisticsGatheredEvent SUMMARY  table_name, row_count, page_count,
                                 column_count, duration_ns   (category: catalog)
LogicalPlanEvent        OPERATOR table_name, node_count, rules_applied
PlanAlternativeEvent    OPERATOR description, access_path, estimated_cost,
                                 estimated_rows, chosen, rejected_because
PhysicalPlanEvent       OPERATOR access_path, estimated_cost, estimated_rows,
                                 candidates_considered, statistics_stale
CostEstimateEvent       VERBOSE  node_id, node_type, io_cost, cpu_cost,
                                 estimated_rows
```

`PlanAlternativeEvent` is emitted **once per candidate, chosen or not**. That is
the point: a planner that reports only its answer cannot be checked, and the
event stream alone should be enough to audit a decision.

`rules_applied` on `LogicalPlanEvent` names only the rules that actually changed
the tree. An early version rebuilt every expression node unconditionally, so
`fold_constants` claimed to fire on every query and the field became noise.

`StatisticsGatheredEvent` is categorised as `catalog`, not `planner`: gathering
is a read of the table and happens whether or not anything is being planned.

---

## Planned events

Specified here, implemented in the milestone that builds the component that
emits them. Nothing below exists yet; stubbing them now would be dead code with
no way to verify the field list is right.

### Milestone 7 — `buffer_pool`

```
BufferPoolEvent       action: hit|miss|pin|unpin|evict|flush,
                      frame_id, page_id, dirty, pin_count
```

### Milestone 8 — `transaction`

```
TransactionEvent      transaction_id,
                      action: begin|commit|abort|rollback_started|rollback_done,
                      isolation_level
UndoRecordEvent       transaction_id, page_id, slot_id, kind, before_image_size
```

### Milestone 9 — `wal`, `recovery`

```
WalAppendEvent        lsn, transaction_id, record_type, page_id, prev_lsn, size
WalFlushEvent         up_to_lsn, bytes_written, duration_ns
CheckpointEvent       lsn, dirty_pages, active_transactions
RecoveryPhaseEvent    phase: analysis|redo|undo, action: started|finished,
                      records_processed, duration_ns
RecoveryActionEvent   phase, lsn, page_id, decision: redo|skip|undo, reason
```

### Milestone 10 — `lock`, `mvcc`

```
LockEvent             transaction_id, resource, mode: shared|exclusive,
                      action: requested|granted|waiting|released|upgraded
DeadlockEvent         cycle, victim_transaction_id, waits_for
VisibilityEvent       transaction_id, page_id, slot_id, visible,
                      xmin, xmax, snapshot_xmin, reason
VersionChainEvent     page_id, slot_id, chain_length, action: append|prune
```

---

## Adding an event

1. Add the frozen dataclass to `engine/diagnostics/events.py` with `category`
   and `level` as `ClassVar`s, and export it from `engine/diagnostics/__init__.py`.
2. Emit it behind a cached tracer flag at the point the work happens.
3. Nothing else. Serialization is generic — `mappers._event_payload` walks the
   dataclass fields — so no mapper change is needed unless a field holds a
   non-primitive.
4. Add the event type to the table above.

`tests/unit/test_diagnostics.py::test_every_event_declares_a_category_and_level`
catches a class that forgets its `ClassVar`s and silently inherits
`LIFECYCLE`/`SUMMARY`.

## Guarantees

- **Sequence numbers are monotonic and gap-free per database.** They double as
  a pagination cursor, so `?after_seq=N` cannot skip or repeat an event even
  while new ones arrive.
- **Retention is bounded.** `CHENDB_TRACE_CAPACITY` (default 20 000) per
  database. Drops are counted and reported, never hidden.
- **Tracing does not change results.** `tests/integration/test_tracing.py`
  asserts the database files produced at `OFF`, `STORAGE` and `VERBOSE` are
  byte-identical.
