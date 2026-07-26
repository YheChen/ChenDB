/**
 * TanStack Query hooks for engine state.
 *
 * Query keys are structured `[resource, databaseId, ...]` so a mutation can
 * invalidate exactly the slice it affected. Inserting a row, for example,
 * changes the records, the page list, the database summary and the event feed —
 * but not the list of databases.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { api, type TraceLevelName } from "@/lib/api";
import type {
  CatalogResponse,
  ColumnSpec,
  DatabaseDetail,
  DatabaseListResponse,
  HealthResponse,
  PageDetailModel,
  PageListResponse,
  RecordsResponse,
  TableDetail,
} from "@/types/api";

export const queryKeys = {
  health: ["health"] as const,
  databases: ["databases"] as const,
  database: (id: string) => ["database", id] as const,
  catalog: (id: string) => ["catalog", id] as const,
  tables: (id: string) => ["tables", id] as const,
  table: (id: string, table: string) => ["table", id, table] as const,
  records: (id: string, table: string, offset: number, limit: number) =>
    ["records", id, table, offset, limit] as const,
  pages: (id: string) => ["pages", id] as const,
  page: (id: string, pageId: number) => ["page", id, pageId] as const,
  trace: (id: string) => ["trace", id] as const,
};

/** Everything that changes when the database is written to. */
function invalidateDatabase(
  client: ReturnType<typeof useQueryClient>,
  databaseId: string,
): void {
  for (const key of [
    queryKeys.database(databaseId),
    queryKeys.catalog(databaseId),
    queryKeys.tables(databaseId),
    queryKeys.pages(databaseId),
    ["table", databaseId],
    ["records", databaseId],
    ["page", databaseId],
  ]) {
    void client.invalidateQueries({ queryKey: key });
  }
}

// -- reads -----------------------------------------------------------------

export function useHealth(): UseQueryResult<HealthResponse> {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: api.health,
    // Doubles as a liveness probe: a failure flips the header to
    // "disconnected" within a few seconds.
    refetchInterval: 5_000,
    retry: false,
  });
}

export function useDatabases(): UseQueryResult<DatabaseListResponse> {
  return useQuery({ queryKey: queryKeys.databases, queryFn: api.listDatabases });
}

export function useDatabase(id: string | null): UseQueryResult<DatabaseDetail> {
  return useQuery({
    queryKey: queryKeys.database(id ?? ""),
    queryFn: () => api.getDatabase(id!),
    enabled: Boolean(id),
  });
}

export function useCatalog(id: string | null): UseQueryResult<CatalogResponse> {
  return useQuery({
    queryKey: queryKeys.catalog(id ?? ""),
    queryFn: () => api.getCatalog(id!),
    enabled: Boolean(id),
  });
}

export function useTable(
  id: string | null,
  table: string | null,
): UseQueryResult<TableDetail> {
  return useQuery({
    queryKey: queryKeys.table(id ?? "", table ?? ""),
    queryFn: () => api.getTable(id!, table!),
    enabled: Boolean(id && table),
    // An unknown table is an expected state, not a transient failure.
    retry: false,
  });
}

export function useRecords(
  id: string | null,
  table: string | null,
  offset: number,
  limit: number,
): UseQueryResult<RecordsResponse> {
  return useQuery({
    queryKey: queryKeys.records(id ?? "", table ?? "", offset, limit),
    queryFn: () => api.listRecords(id!, table!, offset, limit),
    enabled: Boolean(id && table),
    retry: false,
    placeholderData: (previous) => previous,
  });
}

export function usePages(id: string | null): UseQueryResult<PageListResponse> {
  return useQuery({
    queryKey: queryKeys.pages(id ?? ""),
    queryFn: () => api.listPages(id!),
    enabled: Boolean(id),
  });
}

export function usePage(
  id: string | null,
  pageId: number | null,
): UseQueryResult<PageDetailModel> {
  return useQuery({
    queryKey: queryKeys.page(id ?? "", pageId ?? -1),
    queryFn: () => api.getPage(id!, pageId!),
    enabled: Boolean(id) && pageId !== null,
  });
}

// -- writes ----------------------------------------------------------------

export function useCreateDatabase() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: { database_id: string; page_size: number }) =>
      api.createDatabase(payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.databases });
    },
  });
}

export function useDeleteDatabase() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteDatabase(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.databases });
    },
  });
}

export function useCreateTable(databaseId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: { name: string; columns: ColumnSpec[] }) =>
      api.createTable(databaseId, payload),
    onSuccess: () => invalidateDatabase(client, databaseId),
  });
}

export function useInsertRecords(databaseId: string, table: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (rows: unknown[][]) => api.insertRecords(databaseId, table, rows),
    onSuccess: () => invalidateDatabase(client, databaseId),
  });
}

export function useDeleteRecord(databaseId: string, table: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ pageId, slotId }: { pageId: number; slotId: number }) =>
      api.deleteRecord(databaseId, table, pageId, slotId),
    onSuccess: () => invalidateDatabase(client, databaseId),
  });
}

/**
 * Parse SQL. A mutation rather than a query: parsing is an explicit action the
 * user takes (⌘↵), not state to keep in sync. Nothing here invalidates any
 * cache — Milestone 2 parsing has no side effects on the database.
 */
export function useParseSql(databaseId: string) {
  return useMutation({
    mutationFn: (sql: string) => api.parseSql(databaseId, sql),
  });
}

/**
 * Run SQL. A mutation, not a query: executing is an action with side effects
 * (INSERT writes pages), so it must never be re-fetched automatically.
 */
export function useRunQuery(databaseId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ sql, maxRows }: { sql: string; maxRows?: number }) =>
      api.runQuery(databaseId, sql, maxRows),
    onSuccess: (results) => {
      // Only invalidate storage views when something was actually written.
      // A read-only SELECT changes nothing and should not cause a refetch storm.
      if (results.some((result) => result.rows_affected > 0)) {
        invalidateDatabase(client, databaseId);
      }
    },
  });
}

export function useSetTraceLevel(databaseId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (level: TraceLevelName) => api.setTraceLevel(databaseId, level),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.trace(databaseId) });
      void client.invalidateQueries({ queryKey: queryKeys.database(databaseId) });
    },
  });
}
