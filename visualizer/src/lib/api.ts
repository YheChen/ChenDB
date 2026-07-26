/**
 * Typed client for the engine API.
 *
 * Every request funnels through `request()` so error handling, the base path
 * and JSON decoding exist in exactly one place. Types come from
 * `src/types/api.ts`, which is generated from the server's OpenAPI schema —
 * a renamed field on the Python side breaks the TypeScript build rather than
 * failing silently in the browser.
 */

import type {
  CreateDatabaseRequest,
  CatalogResponse,
  ExecutionDetail,
  ExecutionListResponse,
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
} from "@/types/api";

export const API_PREFIX = "/api/v1";

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
] as const;
export type ResumeModeName = (typeof RESUME_MODES)[number];

/** An error carrying the server's structured envelope, when there is one. */
export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

type ErrorBody = {
  detail?: { error?: string; message?: string } | string;
  error?: string;
  message?: string;
};

function extractError(status: number, body: unknown): ApiRequestError {
  const payload = body as ErrorBody | undefined;
  const detail = payload?.detail;
  if (detail && typeof detail === "object") {
    return new ApiRequestError(
      status,
      detail.error ?? "Error",
      detail.message ?? "Request failed",
    );
  }
  if (typeof detail === "string") {
    return new ApiRequestError(status, "Error", detail);
  }
  if (payload?.message) {
    return new ApiRequestError(status, payload.error ?? "Error", payload.message);
  }
  return new ApiRequestError(status, "Error", `Request failed (${status})`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (cause) {
    // A network-level failure means the engine is not running; the UI shows a
    // distinct "disconnected" state rather than a generic error.
    throw new ApiRequestError(
      0,
      "Disconnected",
      "Cannot reach the engine. Is `python -m engine.server` running?",
    );
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const body = text ? JSON.parse(text) : undefined;
  if (!response.ok) throw extractError(response.status, body);
  return body as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
});

export const api = {
  health: () => request<HealthResponse>("/health"),

  listDatabases: () => request<DatabaseListResponse>("/databases"),

  createDatabase: (payload: CreateDatabaseRequest) =>
    request<DatabaseDetail>("/databases", json(payload)),

  getDatabase: (id: string) => request<DatabaseDetail>(`/databases/${id}`),

  deleteDatabase: (id: string) =>
    request<void>(`/databases/${id}`, { method: "DELETE" }),

  getCatalog: (id: string) => request<CatalogResponse>(`/databases/${id}/catalog`),

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

  runQuery: (id: string, sql: string, maxRows?: number) =>
    request<QueryResultModel[]>(
      `/databases/${id}/query`,
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
    request<ExecutionDetail>(`/executions/${executionId}/next`, { method: "POST" }),

  continueExecution: (executionId: string) =>
    request<ExecutionDetail>(`/executions/${executionId}/continue`, {
      method: "POST",
    }),

  resumeExecution: (executionId: string, mode: ResumeModeName, operatorId?: string) =>
    request<ExecutionDetail>(
      `/executions/${executionId}/resume`,
      json({ mode, ...(operatorId ? { operator_id: operatorId } : {}) }),
    ),

  cancelExecution: (executionId: string) =>
    request<ExecutionDetail>(`/executions/${executionId}/cancel`, { method: "POST" }),

  listPages: (id: string) => request<PageListResponse>(`/databases/${id}/pages`),

  getPage: (id: string, pageId: number) =>
    request<PageDetailModel>(`/databases/${id}/pages/${pageId}`),

  listEvents: (id: string, afterSeq: number | null, limit: number) => {
    const cursor = afterSeq === null ? "" : `after_seq=${afterSeq}&`;
    return request<EventsResponse>(`/databases/${id}/events?${cursor}limit=${limit}`);
  },

  clearEvents: (id: string) =>
    request<TraceLevelResponse>(`/databases/${id}/events`, { method: "DELETE" }),

  getTraceLevel: (id: string) =>
    request<TraceLevelResponse>(`/databases/${id}/trace`),

  setTraceLevel: (id: string, level: TraceLevelName) =>
    request<TraceLevelResponse>(`/databases/${id}/trace`, {
      method: "PUT",
      body: JSON.stringify({ level }),
    }),
};

/** Absolute WebSocket URL for a database's live event stream. */
export function eventStreamUrl(databaseId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${API_PREFIX}/databases/${databaseId}/events/stream`;
}
