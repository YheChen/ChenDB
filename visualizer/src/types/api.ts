/**
 * Generated from the engine's OpenAPI schema. Do not edit by hand.
 *
 *   python scripts/generate_api_types.py
 *
 * The source of truth is the Pydantic models in engine/server/schemas/.
 */

/** One AST node in the flattened tree. */
export interface AstNodeModel {
  node_id: number;
  /** Class name, e.g. 'BinaryOp' */
  node_type: string;
  start: number;
  end: number;
  line: number;
  column: number;
  /** The source fragment this node was parsed from */
  text: string;
  /** node_ids of direct children, in order */
  children: number[];
  /** Scalar fields: operator, name, value, data_type, ... */
  attributes: Record<string, unknown>;
  /** Short display label: the operator, name or value if it has one */
  label: string;
}

export interface AstTreeModel {
  nodes: AstNodeModel[];
  /** One per statement, in source order */
  root_ids: number[];
}

export interface CatalogResponse {
  tables: TableSummary[];
  system_tables: TableSummary[];
  /** Id the next created table or index will receive; one sequence serves both, as PostgreSQL's OID counter does */
  next_object_id: number;
  stats: CatalogStatsModel;
}

/** Catalog cache effectiveness. A miss costs two full catalog scans. */
export interface CatalogStatsModel {
  lookups: number;
  cache_hits: number;
  hit_rate: number;
  scans: number;
  tables_created: number;
  indexes_created: number;
}

export interface ColumnModel {
  name: string;
  type: "INTEGER" | "FLOAT" | "BOOLEAN" | "TEXT";
  nullable: boolean;
  primary_key: boolean;
  /** Encoded width in bytes, or null for variable-width types */
  fixed_size: null | number;
}

export interface ColumnSpec {
  name: string;
  type: "INTEGER" | "FLOAT" | "BOOLEAN" | "TEXT";
  nullable?: boolean;
  primary_key?: boolean;
}

export interface CreateDatabaseRequest {
  /** Workspace-relative identifier. Never a filesystem path. */
  database_id: string;
  /** Bytes per page; must be a power of two. Small values are useful for demos because they force page chaining after a few rows. */
  page_size?: number;
}

/**
 * Programmatic index creation.
 * 
 * ``CREATE INDEX`` through ``POST /query`` is the primary path; this stays for
 * clients that would rather not build SQL strings, matching the table endpoint.
 */
export interface CreateIndexRequest {
  name: string;
  table: string;
  column: string;
  unique?: boolean;
}

/**
 * Programmatic table creation.
 * 
 * ``CREATE TABLE`` through ``POST /query`` is the primary path from Milestone 3
 * onward; this stays for clients that would rather not build SQL strings.
 */
export interface CreateTableRequest {
  name: string;
  columns: ColumnSpec[];
}

export interface DatabaseDetail {
  database_id: string;
  page_size: number;
  page_count: number;
  file_size_bytes: number;
  format_version: number;
  /** User tables. System tables are listed by /catalog. */
  table_names: string[];
  table_count: number;
  free_list_head: null | number;
  stats: PagerStatsModel;
  trace_level: string;
}

export interface DatabaseListResponse {
  databases: DatabaseSummary[];
  workspace: string;
}

export interface DatabaseSummary {
  database_id: string;
  size_bytes: number;
  modified_ns: number;
  is_open: boolean;
}

export interface DeleteRecordResponse {
  deleted: boolean;
  record_id: RecordIdModel;
}

export type EventCategory = "lifecycle" | "storage" | "record" | "parser" | "operator" | "catalog" | "index" | "planner" | "buffer_pool" | "transaction" | "wal" | "recovery" | "lock" | "mvcc";

export interface EventsResponse {
  events: TraceRecordModel[];
  stats: TraceStatsModel;
  page: PageInfo;
}

/** A stepped execution's current state, including where it is paused. */
export interface ExecutionDetail {
  execution_id: string;
  database_id: string;
  sql: string;
  statement_kind: string;
  state: "pending" | "running" | "paused" | "finished" | "cancelled" | "failed";
  steps_taken: number;
  /** operator_open, operator_next, row_emitted, operator_close or page_read */
  pause_kind: null | string;
  /** Which operator is at the checkpoint */
  pause_operator_id: null | string;
  /** The row or page involved, rendered for display */
  pause_detail: string;
  /** Available once the operators have been built */
  plan: PlanModel | null;
  /** The row at the checkpoint, when paused on one */
  current_row: null | unknown[];
  /** Rows emitted from the root up to this point */
  rows_so_far: number;
  /** Null until the execution has finished */
  result: QueryResultModel | null;
  error: string;
  age_seconds: number;
  idle_seconds: number;
}

export interface ExecutionListResponse {
  executions: ExecutionSummary[];
  max_executions: number;
}

export interface ExecutionSummary {
  execution_id: string;
  database_id: string;
  statement_kind: string;
  state: "pending" | "running" | "paused" | "finished" | "cancelled" | "failed";
  steps_taken: number;
  age_seconds: number;
  idle_seconds: number;
}

export interface FieldLayoutModel {
  index: number;
  name: string;
  type_name: string;
  is_null: boolean;
  /** -1 for NULL, which occupies no bytes */
  offset: number;
  length: number;
  value: unknown;
}

/** One decoded header field, with the bytes it was read from. */
export interface HeaderFieldModel {
  name: string;
  offset: number;
  size: number;
  value: number | string;
  raw_hex: string;
  description: string;
}

export interface HealthResponse {
  engine_version: string;
  api_version: string;
  /** Highest completed milestone */
  milestone: number;
  /** Workspace directory name, not a full path */
  workspace: string;
  open_databases: number;
  /** Which capabilities exist in this build. The UI hides panels whose feature is false rather than showing controls that cannot work. */
  features: Record<string, boolean>;
}

export interface IndexDetail {
  index: IndexSummary;
  tree: TreeSnapshotModel;
  stats: IndexStatsModel;
}

export interface IndexListResponse {
  indexes: IndexSummary[];
}

/**
 * One traced point lookup.
 * 
 * ``path`` is what the tree view highlights: the page ids from root to leaf.
 * ``pages_visited`` can exceed its length when duplicates spill across leaves
 * and the search has to step right — which is exactly the case worth seeing.
 */
export interface IndexSearchResponse {
  index_name: string;
  value: string;
  found: boolean;
  /** '(page,slot)' per matching row */
  matches: string[];
  /** Page ids from the root to the leaf reached */
  path: number[];
  pages_visited: number;
  height: number;
}

/** What the index has done since the database was opened. */
export interface IndexStatsModel {
  searches: number;
  inserts: number;
  deletes: number;
  splits: number;
  root_splits: number;
  range_scans: number;
  nodes_visited: number;
  leaves_visited: number;
  pages_allocated: number;
}

export interface IndexSummary {
  index_id: number;
  name: string;
  table_name: string;
  column_name: string;
  /** Which column of the record the key comes from */
  column_position: number;
  data_type: string;
  unique: boolean;
  /** Page the tree is rooted at. Changes when the root splits. */
  root_page: number;
  /** Levels from root to leaf; 1 for a single leaf */
  height: number;
  entry_count: number;
  page_count: number;
}

export interface InsertRecordsRequest {
  /** Positional values per row, in column order */
  rows: unknown[][];
}

export interface InsertRecordsResponse {
  inserted: number;
  record_ids: RecordIdModel[];
  pages_allocated: number;
  duration_ns: number;
}

/**
 * One node of the physical plan: what it will cost, and what it did.
 * 
 * Estimated and actual sit side by side because the gap between them is the
 * single most useful thing a plan view can show. A plan that is slow is almost
 * always a plan whose row estimate was wrong, and no amount of staring at the
 * chosen operators reveals that — only the comparison does.
 */
export interface OperatorNodeModel {
  operator_id: string;
  /** SeqScan, IndexScan, Filter or Project */
  operator_type: string;
  /** Predicate, table, index condition or projection */
  detail: string;
  /** operator_ids of inputs, left to right */
  children: string[];
  output_columns: ResultColumnModel[];
  /** Rows the planner expected out; null if the plan was not costed */
  estimated_rows: null | number;
  /** Cumulative estimated cost of this node and everything below it */
  estimated_cost: null | number;
  /** The I/O half of this node's own cost */
  estimated_io_cost: null | number;
  /** The CPU half of this node's own cost */
  estimated_cpu_cost: null | number;
  /** Times this operator was asked for a row */
  next_calls: number;
  /** Rows it consumed from its children */
  input_rows: number;
  /** Rows it produced */
  output_rows: number;
  /** Filter only: rows whose predicate was not TRUE; 0 elsewhere */
  rows_rejected: number;
  /** Time spent inside this operator's own work */
  duration_ns: number;
}

export interface PageDetailModel {
  summary: PageSummaryModel;
  header_fields: HeaderFieldModel[];
  slots: SlotDetailModel[];
  /** The entire page, hex-encoded */
  raw_hex: string;
  page_size: number;
  header_size: number;
  slot_directory_end: number;
  free_start: number;
  free_end: number;
}

/** Cursor pagination for the event stream. */
export interface PageInfo {
  after_seq: null | number;
  returned: number;
  /** Pass as after_seq to continue; null when caught up */
  next_cursor: null | number;
  has_more: boolean;
}

export interface PageListResponse {
  pages: PageSummaryModel[];
  page_size: number;
  page_count: number;
  total_bytes: number;
}

export interface PageSummaryModel {
  page_id: number;
  /** META, HEAP, SCHEMA, FREE, ... */
  page_type: string;
  /** page_id * page_size */
  file_offset: number;
  /** Always 0 until Milestone 9 adds the WAL */
  lsn: number;
  checksum: number;
  checksum_valid: boolean;
  /** Directory entries, tombstones included */
  slot_count: number;
  live_record_count: number;
  /** Contiguous bytes between the two regions */
  free_space: number;
  /** Bytes compaction would recover */
  reclaimable_space: number;
  next_page_id: null | number;
  /** Table name, or 'meta' / 'schema' / 'unallocated' */
  owner: string;
  error?: null | string;
  /** Always false in Milestone 1: without a buffer pool every write goes straight through, so no page is ever cached-and-dirty. */
  dirty?: boolean;
}

/** Cumulative I/O counters since the database handle was opened. */
export interface PagerStatsModel {
  page_reads: number;
  page_writes: number;
  allocations: number;
  recycled_allocations: number;
  frees: number;
  syncs: number;
  bytes_read: number;
  bytes_written: number;
  read_time_ns: number;
  write_time_ns: number;
}

export interface ParseRequest {
  /** One or more statements, separated by semicolons */
  sql: string;
}

/**
 * Tokens, AST and error together — all three are partial-result friendly.
 * 
 * ``tokens`` can be non-empty while ``statements`` is empty and ``error`` is
 * set: that is a half-typed query, the normal state of one being written.
 */
export interface ParseResponse {
  sql: string;
  ok: boolean;
  tokens: TokenModel[];
  ast: AstTreeModel;
  statements: StatementModel[];
  error?: SqlErrorModel | null;
  /** False when tokenizing failed, so `tokens` is truncated */
  lexed_ok: boolean;
  token_count: number;
  node_count: number;
  duration_ns: number;
}

/** One access path the planner considered, chosen or not. */
export interface PlanAlternativeModel {
  description: string;
  /** PhysicalSeqScan or PhysicalIndexScan */
  access_path: string;
  estimated_cost: number;
  estimated_rows: number;
  chosen: boolean;
  /** Why this lost, e.g. '3.6x the cost of the chosen plan'. Empty for the winner. */
  rejected_because: string;
  index_name: null | string;
}

export interface PlanModel {
  nodes: OperatorNodeModel[];
  root_id: string;
  /** Every access path considered, with the cost of each */
  alternatives: PlanAlternativeModel[];
  /** Rewrite rules that changed the plan, in the order they ran */
  rewrites: string[];
  /** Total cost of the chosen plan */
  estimated_cost: null | number;
  statistics: PlanStatisticsModel | null;
}

/** The statistics the estimates were computed from. */
export interface PlanStatisticsModel {
  table_name: string;
  row_count: number;
  page_count: number;
  /** True when the table was written to after it was last analyzed, so every estimate above is based on old numbers */
  stale: boolean;
  gathered_at_ns: number;
}

export interface QueryRequest {
  /** One or more statements, separated by semicolons */
  sql: string;
  /** Override the row ceiling */
  max_rows?: null | number;
}

/** The outcome of one statement. */
export interface QueryResultModel {
  /** SelectStatement, InsertStatement, CreateTableStatement or CreateIndexStatement */
  statement_kind: string;
  returns_rows: boolean;
  /** Summary for statements that return no rows */
  message: string;
  columns: ResultColumnModel[];
  rows: unknown[][];
  /** Where each row lives; empty when the projection computes values */
  record_ids: RecordIdModel[];
  /** Null for statements with no operator tree */
  plan: PlanModel | null;
  rows_returned: number;
  /** Rows written, for INSERT */
  rows_affected: number;
  /** Rows the scan produced before filtering */
  rows_scanned: number;
  /** Rows a filter dropped */
  rows_rejected: number;
  pages_read: number;
  pages_written: number;
  duration_ns: number;
  /** True when the row ceiling cut the result short */
  truncated: boolean;
  cancelled: boolean;
}

/** PostgreSQL calls this a ctid: the physical address of a row. */
export interface RecordIdModel {
  page_id: number;
  slot_id: number;
}

export interface RecordLayoutModel {
  values: unknown[];
  fields: FieldLayoutModel[];
  null_bitmap_hex: string;
  /** One entry per column; true means NULL */
  null_bitmap_bits: boolean[];
  null_bitmap_size: number;
  total_size: number;
}

export interface RecordsResponse {
  columns: ColumnModel[];
  rows: RowModel[];
  offset: number;
  limit: number;
  returned: number;
  has_more: boolean;
  /** Rows the heap scan touched, including those skipped by offset */
  rows_scanned: number;
  /** Page reads this request caused */
  pages_read: number;
  duration_ns: number;
}

export interface ResultColumnModel {
  name: string;
  /** SQL type, or null when an expression's type is not statically known */
  type: null | string;
}

export interface ResumeRequest {
  mode?: "step" | "continue" | "until_row" | "until_page_read" | "until_operator";
  /** Required by until_operator; ignored otherwise */
  operator_id?: null | string;
}

export interface RowModel {
  record_id: RecordIdModel;
  /** Positional values matching the table's column order; null is NULL */
  values: unknown[];
}

export interface SchemaModel {
  columns: ColumnModel[];
  /** Bytes of null bitmap per record */
  null_bitmap_size: number;
  /** Constant encoded row size, or null if any column varies */
  fixed_row_size: null | number;
}

export interface SetTraceLevelRequest {
  level: "OFF" | "SUMMARY" | "OPERATOR" | "STORAGE" | "VERBOSE";
}

export interface SlotDetailModel {
  slot_id: number;
  offset: number;
  length: number;
  is_live: boolean;
  raw_hex: string;
  record?: RecordLayoutModel | null;
  decode_error?: null | string;
}

/** A lex or parse failure, positioned for an editor marker. */
export interface SqlErrorModel {
  /** LexError, ParseError or UnsupportedSqlError */
  kind: string;
  message: string;
  start: number;
  end: number;
  line: number;
  column: number;
  /** What the parser would have accepted */
  expected?: string[];
  /** What it saw instead */
  found?: string;
}

/** One parsed statement. */
export interface StatementModel {
  root_id: number;
  /** Statement node type, e.g. 'SelectStatement' */
  kind: string;
  start: number;
  end: number;
  text: string;
}

export interface StepRequest {
  /** Exactly one statement. Stepping a script is refused. */
  sql: string;
}

export interface TableDetail {
  table_id: number;
  name: string;
  is_system: boolean;
  schema: SchemaModel;
  columns: ColumnModel[];
  storage: TableStorageModel;
}

/** What a table costs on disk. Computed, not cached. */
export interface TableStorageModel {
  first_page: number;
  last_page: number;
  page_ids: number[];
  page_count: number;
  /** Live rows. O(pages) to compute — no cached count. */
  row_count: number;
  /** page_count * page_size */
  bytes_allocated: number;
  /** Contiguous free bytes across the table's pages */
  free_space: number;
  /** Bytes held by tombstoned rows, recoverable by compaction */
  reclaimable_space: number;
}

export interface TableSummary {
  table_id: number;
  name: string;
  column_count: number;
  row_count: number;
  page_count: number;
  /** True for chendb_* tables, which belong to the engine */
  is_system: boolean;
}

/** One token, with the source range it covers. */
export interface TokenModel {
  index: number;
  /** Token category: keyword, identifier, int_literal, ... */
  type: string;
  /** Exact source text, before unescaping */
  lexeme: string;
  /** Character offset of the first character */
  start: number;
  /** Character offset one past the last */
  end: number;
  line: number;
  column: number;
  /** Set when type is 'keyword' */
  keyword?: null | string;
  /** Decoded value, for literals */
  value?: unknown;
}

export interface TraceLevelResponse {
  level: "OFF" | "SUMMARY" | "OPERATOR" | "STORAGE" | "VERBOSE";
  stats: TraceStatsModel;
}

/** One diagnostic event with the envelope the tracer stamped on it. */
export interface TraceRecordModel {
  /** Monotonic per database; also the pagination cursor */
  seq: number;
  timestamp_ns: number;
  category: "lifecycle" | "storage" | "record" | "parser" | "operator" | "catalog" | "index" | "planner" | "buffer_pool" | "transaction" | "wal" | "recovery" | "lock" | "mvcc";
  level: "OFF" | "SUMMARY" | "OPERATOR" | "STORAGE" | "VERBOSE";
  /** Event class name, e.g. 'PageReadEvent' */
  event_type: string;
  /** Flat payload; fields depend on event_type */
  event: Record<string, unknown>;
}

/** Retention state, so a client can tell when it has missed events. */
export interface TraceStatsModel {
  capacity: number;
  size: number;
  total_recorded: number;
  /** Events evicted from the ring buffer before being read */
  dropped: number;
  level: "OFF" | "SUMMARY" | "OPERATOR" | "STORAGE" | "VERBOSE";
}

/** One B+ tree node, decoded for display. */
export interface TreeNodeModel {
  page_id: number;
  /** 0 at the leaves, increasing toward the root */
  level: number;
  is_leaf: boolean;
  /** Rendered keys or separators, in slot order. '-∞' is the sentinel every internal node starts with. */
  keys: string[];
  /** Child page ids; empty for a leaf */
  children: number[];
  /** '(page,slot)' per entry; empty for an internal node */
  record_ids: string[];
  /** The next leaf in key order, or null at the end of the chain */
  next_leaf_id: null | number;
  free_bytes: number;
  entry_count: number;
}

export interface TreeSnapshotModel {
  root_page_id: number;
  height: number;
  nodes: TreeNodeModel[];
  /** True when the node budget was hit and the tree is only partly sent */
  truncated: boolean;
}
