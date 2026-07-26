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
 * Milestone 1 has no SQL parser, so tables are defined structurally.
 * 
 * Milestone 2 adds ``POST /query`` with ``CREATE TABLE``; this endpoint stays
 * as the programmatic path.
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
  table_name: null | string;
  schema?: SchemaModel | null;
  /** Live rows; null when no table is defined yet */
  row_count: null | number;
  heap_page_ids: number[];
  schema_page_ids: number[];
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

export interface TableResponse {
  name: string;
  schema: SchemaModel;
  row_count: number;
  heap_page_ids: number[];
  first_page_id: number;
  last_page_id: number;
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
