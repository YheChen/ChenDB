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

Sixteen types across four categories. These are the only events that exist
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

`RecordReadEvent` is `VERBOSE` because a scan would otherwise emit one event
per row — exactly the flood trace levels exist to prevent.

---

## Planned events

Specified here, implemented in the milestone that builds the component that
emits them. Nothing below exists yet; stubbing them now would be dead code with
no way to verify the field list is right.

### Milestone 3 — `operator`

```
OperatorEvent         operator_id, operator_type,
                      action: opened|next|row_emitted|closed,
                      input_rows, output_rows
ExpressionEvalEvent   operator_id, expression, inputs, result   VERBOSE
```

### Milestone 4 — `catalog`

```
CatalogLookupEvent    object_type, name, found, page_id
TableCreatedEvent     table_name, column_count, root_page_id
```

### Milestone 5 — `index`

```
IndexSearchEvent      index_name, key, found, pages_visited, depth
IndexDescentEvent     index_name, page_id, level, child_page_id  VERBOSE
NodeSplitEvent        index_name, page_id, new_page_id, level,
                      promoted_key, is_root_split
NodeMergeEvent        index_name, page_id, sibling_page_id, level
RangeScanEvent        index_name, low, high, leaves_visited, rows_emitted
```

### Milestone 6 — `planner`

```
LogicalPlanEvent      plan_id, root_node_type, node_count
PhysicalPlanEvent     plan_id, chosen, estimated_cost, estimated_rows
PlanAlternativeEvent  plan_id, description, estimated_cost, rejected_because
CostEstimateEvent     node_id, cardinality, io_cost, cpu_cost
```

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
