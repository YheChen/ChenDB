/**
 * Typed client for the engine API — the vocabulary, not the plumbing.
 *
 * One method per endpoint, and types from `src/types/api.ts`, which is
 * generated from the server's OpenAPI schema: a renamed field on the Python
 * side breaks the TypeScript build rather than failing silently in a browser.
 *
 * *How* a request travels is deliberately not here. `request()` hands it to
 * whichever `Transport` is active — `fetch` against a running server, or the
 * same ASGI app compiled to WebAssembly and running in the tab. See
 * `./transport.ts`; this file should read the same either way.
 */

import { getTransport } from "./transport";
import type {
  BufferPoolResponse,
  CreateDatabaseRequest,
  CatalogResponse,
  ExecutionDetail,
  ExecutionListResponse,
  CreateIndexRequest,
  IndexDetail,
  IndexListResponse,
  IndexSearchResponse,
  ParseResponse,
  QueryResultModel,
  TableDetail,
  TableSummary,
  CreateTableRequest,
  DatabaseDetail,
  DatabaseListResponse,
  DeleteRecordResponse,
  EventsResponse,
  HealthResponse,
  InsertRecordsResponse,
  PageDetailModel,
  PageListResponse,
  RecordsResponse,
  TraceLevelResponse,
  TransactionListResponse,
  TransactionResultResponse,
  CheckpointResponse,
  CrashResponse,
  RecoveryReportModel,
  WalResponse,
  LockTableResponse,
  SessionListResponse,
} from "@/types/api";

export {
  API_PREFIX,
  API_VERSION,
  ApiRequestError,
  eventStreamUrl,
  type ConnectionState,
  type Transport,
} from "./transport";

/** Trace levels, ordered. Mirrors engine.diagnostics.levels.TraceLevel. */
export const TRACE_LEVELS = [
  "OFF",
  "SUMMARY",
  "OPERATOR",
  "STORAGE",
  "VERBOSE",
] as const;
export type TraceLevelName = (typeof TRACE_LEVELS)[number];

/** How far a stepped execution runs before pausing again. */
export const RESUME_MODES = [
  "step",
  "continue",
  "until_row",
  "until_page_read",
  "until_operator",
  "until_index_operation",
] as const;
export type ResumeModeName = (typeof RESUME_MODES)[number];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return getTransport().request<T>(path, init);
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
});

/**
 * ``?session=alice``, or nothing at all.
 *
 * Omitted rather than sent as ``default``, so a request from a single-console
 * view looks in the log exactly as it did before Milestone 10 — the server's
 * default and this one's absence have to mean the same thing, and the cheapest
 * way to guarantee that is to have only one of them.
 */
function qs(session?: string): string {
  return session ? `?session=${encodeURIComponent(session)}` : "";
}

export const api = {
  health: () => request<HealthResponse>("/health"),

  listDatabases: () => request<DatabaseListResponse>("/databases"),

  createDatabase: (payload: CreateDatabaseRequest) =>
    request<DatabaseDetail>("/databases", json(payload)),

  getDatabase: (id: string) => request<DatabaseDetail>(`/databases/${id}`),

  deleteDatabase: (id: string) =>
    request<void>(`/databases/${id}`, { method: "DELETE" }),

  getCatalog: (id: string) =>
    request<CatalogResponse>(`/databases/${id}/catalog`),

  listTables: (id: string, includeSystem = false) =>
    request<TableSummary[]>(
      `/databases/${id}/tables${includeSystem ? "?include_system=true" : ""}`,
    ),

  getTable: (id: string, table: string) =>
    request<TableDetail>(`/databases/${id}/tables/${table}`),

  createTable: (id: string, payload: CreateTableRequest) =>
    request<TableDetail>(`/databases/${id}/tables`, json(payload)),

  listRecords: (id: string, table: string, offset: number, limit: number) =>
    request<RecordsResponse>(
      `/databases/${id}/tables/${table}/records?offset=${offset}&limit=${limit}`,
    ),

  insertRecords: (id: string, table: string, rows: unknown[][]) =>
    request<InsertRecordsResponse>(
      `/databases/${id}/tables/${table}/records`,
      json({ rows }),
    ),

  deleteRecord: (id: string, table: string, pageId: number, slotId: number) =>
    request<DeleteRecordResponse>(
      `/databases/${id}/tables/${table}/records/${pageId}/${slotId}`,
      { method: "DELETE" },
    ),

  parseSql: (id: string, sql: string) =>
    request<ParseResponse>(`/databases/${id}/parse`, json({ sql })),

  runQuery: (id: string, sql: string, maxRows?: number, session?: string) =>
    request<QueryResultModel[]>(
      `/databases/${id}/query${qs(session)}`,
      json({ sql, ...(maxRows ? { max_rows: maxRows } : {}) }),
    ),

  startSteppedQuery: (id: string, sql: string) =>
    request<ExecutionDetail>(`/databases/${id}/query/step`, json({ sql })),

  getExecution: (executionId: string) =>
    request<ExecutionDetail>(`/executions/${executionId}`),

  listExecutions: (databaseId?: string) =>
    request<ExecutionListResponse>(
      `/executions${databaseId ? `?database_id=${databaseId}` : ""}`,
    ),

  stepExecution: (executionId: string) =>
    request<ExecutionDetail>(`/executions/${executionId}/next`, {
      method: "POST",
    }),

  continueExecution: (executionId: string) =>
    request<ExecutionDetail>(`/executions/${executionId}/continue`, {
      method: "POST",
    }),

  resumeExecution: (
    executionId: string,
    mode: ResumeModeName,
    operatorId?: string,
  ) =>
    request<ExecutionDetail>(
      `/executions/${executionId}/resume`,
      json({ mode, ...(operatorId ? { operator_id: operatorId } : {}) }),
    ),

  cancelExecution: (executionId: string) =>
    request<ExecutionDetail>(`/executions/${executionId}/cancel`, {
      method: "POST",
    }),

  listIndexes: (id: string, table?: string) =>
    request<IndexListResponse>(
      `/databases/${id}/indexes${table ? `?table=${encodeURIComponent(table)}` : ""}`,
    ),

  getIndex: (id: string, name: string, maxNodes = 512) =>
    request<IndexDetail>(
      `/databases/${id}/indexes/${encodeURIComponent(name)}?max_nodes=${maxNodes}`,
    ),

  createIndex: (id: string, payload: CreateIndexRequest) =>
    request<IndexDetail>(`/databases/${id}/indexes`, json(payload)),

  searchIndex: (id: string, name: string, value: string) =>
    request<IndexSearchResponse>(
      `/databases/${id}/indexes/${encodeURIComponent(name)}/search` +
        `?value=${encodeURIComponent(value)}`,
    ),

  getBufferPool: (id: string) =>
    request<BufferPoolResponse>(`/databases/${id}/buffer-pool`),

  getLocks: (id: string) =>
    request<LockTableResponse>(`/databases/${id}/locks`),

  getSessions: (id: string) =>
    request<SessionListResponse>(`/databases/${id}/sessions`),

  vacuum: (id: string) =>
    request<CheckpointResponse>(`/databases/${id}/vacuum`, { method: "POST" }),

  getWal: (id: string, limit = 200) =>
    request<WalResponse>(`/databases/${id}/wal?limit=${limit}`),

  getRecovery: (id: string) =>
    request<RecoveryReportModel>(`/databases/${id}/recovery`),

  checkpoint: (id: string) =>
    request<CheckpointResponse>(`/databases/${id}/checkpoint`, {
      method: "POST",
    }),

  crash: (id: string) =>
    request<CrashResponse>(`/databases/${id}/crash`, { method: "POST" }),

  getTransactions: (id: string, session?: string) =>
    request<TransactionListResponse>(
      `/databases/${id}/transactions${qs(session)}`,
    ),

  beginTransaction: (id: string, session?: string) =>
    request<TransactionResultResponse>(
      `/databases/${id}/transactions${qs(session)}`,
      { method: "POST" },
    ),

  commitTransaction: (id: string, session?: string) =>
    request<TransactionResultResponse>(
      `/databases/${id}/transactions/commit${qs(session)}`,
      { method: "POST" },
    ),

  rollbackTransaction: (id: string, session?: string) =>
    request<TransactionResultResponse>(
      `/databases/${id}/transactions/rollback${qs(session)}`,
      { method: "POST" },
    ),

  listPages: (id: string) =>
    request<PageListResponse>(`/databases/${id}/pages`),

  getPage: (id: string, pageId: number) =>
    request<PageDetailModel>(`/databases/${id}/pages/${pageId}`),

  listEvents: (id: string, afterSeq: number | null, limit: number) => {
    const cursor = afterSeq === null ? "" : `after_seq=${afterSeq}&`;
    return request<EventsResponse>(
      `/databases/${id}/events?${cursor}limit=${limit}`,
    );
  },

  clearEvents: (id: string) =>
    request<TraceLevelResponse>(`/databases/${id}/events`, {
      method: "DELETE",
    }),

  getTraceLevel: (id: string) =>
    request<TraceLevelResponse>(`/databases/${id}/trace`),

  setTraceLevel: (id: string, level: TraceLevelName) =>
    request<TraceLevelResponse>(`/databases/${id}/trace`, {
      method: "PUT",
      body: JSON.stringify({ level }),
    }),
};
