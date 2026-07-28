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
  BufferPoolResponse,
  CatalogResponse,
  ColumnSpec,
  CreateIndexRequest,
  IndexDetail,
  IndexListResponse,
  IndexSearchResponse,
  DatabaseDetail,
  DatabaseListResponse,
  HealthResponse,
  PageDetailModel,
  PageListResponse,
  RecordsResponse,
  TableDetail,
  TransactionListResponse,
  RecoveryReportModel,
  WalResponse,
  LockTableResponse,
  SessionListResponse,
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
  indexes: (id: string, table?: string) =>
    ["indexes", id, table ?? "*"] as const,
  index: (id: string, name: string, maxNodes: number) =>
    ["index", id, name, maxNodes] as const,
  indexSearch: (id: string, name: string, value: string) =>
    ["indexSearch", id, name, value] as const,
  bufferPool: (id: string) => ["bufferPool", id] as const,
  transactions: (id: string) => ["transactions", id] as const,
  wal: (id: string, limit: number) => ["wal", id, limit] as const,
  recovery: (id: string) => ["recovery", id] as const,
  locks: (id: string) => ["locks", id] as const,
  sessions: (id: string) => ["sessions", id] as const,
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
    queryKeys.bufferPool(databaseId),
    queryKeys.transactions(databaseId),
    ["wal", databaseId],
    queryKeys.recovery(databaseId),
    queryKeys.locks(databaseId),
    queryKeys.sessions(databaseId),
    queryKeys.indexes(databaseId),
    ["indexes", databaseId],
    ["index", databaseId],
    ["indexSearch", databaseId],
    ["table", databaseId],
    ["records", databaseId],
    ["page", databaseId],
  ]) {
    void client.invalidateQueries({ queryKey: key });
  }
}

// -- reads -----------------------------------------------------------------

/**
 * Whether the running build has a feature, defaulting to *present*.
 *
 * The default matters. `/health` takes a moment to arrive, and treating an
 * unanswered question as "absent" would make every panel flicker off and back
 * on at load. A feature is only hidden once the engine has actually said it is
 * missing — which is what the WASM build does for step mode, because there is
 * no thread there for a paused execution to sit on.
 */
export function useFeature(name: string): boolean {
  const health = useHealth();
  const features = health.data?.features as Record<string, boolean> | undefined;
  return features?.[name] ?? true;
}

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
  return useQuery({
    queryKey: queryKeys.databases,
    queryFn: api.listDatabases,
  });
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

export function useIndexes(
  id: string | null,
  table?: string,
): UseQueryResult<IndexListResponse> {
  return useQuery({
    queryKey: queryKeys.indexes(id ?? "", table),
    queryFn: () => api.listIndexes(id!, table),
    enabled: Boolean(id),
  });
}

export function useIndex(
  id: string | null,
  name: string | null,
  maxNodes = 512,
): UseQueryResult<IndexDetail> {
  return useQuery({
    queryKey: queryKeys.index(id ?? "", name ?? "", maxNodes),
    queryFn: () => api.getIndex(id!, name!, maxNodes),
    enabled: Boolean(id && name),
    // A dropped index is an expected state, not a transient failure.
    retry: false,
  });
}

/**
 * Trace one point lookup. Only enabled once a value is typed, so opening the
 * panel does not run a search nobody asked for.
 */
export function useIndexSearch(
  id: string | null,
  name: string | null,
  value: string,
): UseQueryResult<IndexSearchResponse> {
  return useQuery({
    queryKey: queryKeys.indexSearch(id ?? "", name ?? "", value),
    queryFn: () => api.searchIndex(id!, name!, value),
    enabled: Boolean(id && name && value !== ""),
    retry: false,
  });
}

/**
 * The frame grid. Polled rather than pushed: the pool changes on *every* page
 * read, so streaming it would be a firehose, and a cache's interesting
 * behaviour — a working set settling in, a scan wiping it out — is visible at
 * a human refresh rate anyway.
 */
export function useBufferPool(
  id: string | null,
  { refetchInterval = 1000 }: { refetchInterval?: number | false } = {},
): UseQueryResult<BufferPoolResponse> {
  return useQuery({
    queryKey: queryKeys.bufferPool(id ?? ""),
    queryFn: () => api.getBufferPool(id!),
    enabled: Boolean(id),
    refetchInterval,
  });
}

/**
 * The transaction timeline and the open transaction's undo log.
 *
 * Polled for the same reason the pool is: the undo log grows on every write to
 * a page it has not seen yet, and a stream of that would be noise. Unlike the
 * pool, this is also *correctness*-relevant — a forgotten open transaction is
 * something the user must be able to see — so it keeps polling even when the
 * transactions workspace is not on screen, and the top bar reads from the same
 * cache entry.
 */
export function useTransactions(
  id: string | null,
  {
    refetchInterval = 1500,
    session,
  }: { refetchInterval?: number | false; session?: string } = {},
): UseQueryResult<TransactionListResponse> {
  return useQuery({
    queryKey: [...queryKeys.transactions(id ?? ""), session ?? "default"],
    queryFn: () => api.getTransactions(id!, session),
    enabled: Boolean(id),
    refetchInterval,
    // Unlike the pool, this keeps polling in a background tab. An open
    // transaction is state the user is responsible for ending, and finding out
    // it exists only when the tab regains focus is too late to be useful.
    refetchIntervalInBackground: true,
  });
}

/**
 * The log, as a window onto its most recent records.
 *
 * Polled rather than streamed, for the reason the pool is: there is one record
 * per page write, so a stream of them would be the busiest thing in the app and
 * the interesting shape — the log filling up, then collapsing at a checkpoint —
 * is perfectly legible at a human refresh rate.
 */
export function useWal(
  id: string | null,
  {
    limit = 200,
    refetchInterval = 1500,
  }: { limit?: number; refetchInterval?: number | false } = {},
): UseQueryResult<WalResponse> {
  return useQuery({
    queryKey: queryKeys.wal(id ?? "", limit),
    queryFn: () => api.getWal(id!, limit),
    enabled: Boolean(id),
    refetchInterval,
  });
}

/** What the last open had to repair. Static until the database is reopened. */
export function useRecovery(
  id: string | null,
): UseQueryResult<RecoveryReportModel> {
  return useQuery({
    queryKey: queryKeys.recovery(id ?? ""),
    queryFn: () => api.getRecovery(id!),
    enabled: Boolean(id),
  });
}

/**
 * The lock table and the wait-for graph.
 *
 * Polled fast — a lock that is held for two hundred milliseconds is invisible
 * at a one-second refresh, and short-lived contention is most of what there is
 * to see with two consoles driven by hand.
 */
export function useLocks(
  id: string | null,
  { refetchInterval = 600 }: { refetchInterval?: number | false } = {},
): UseQueryResult<LockTableResponse> {
  return useQuery({
    queryKey: queryKeys.locks(id ?? ""),
    queryFn: () => api.getLocks(id!),
    enabled: Boolean(id),
    refetchInterval,
    refetchIntervalInBackground: true,
  });
}

/** Every session's transaction, snapshot and lock count. */
export function useSessions(
  id: string | null,
  { refetchInterval = 600 }: { refetchInterval?: number | false } = {},
): UseQueryResult<SessionListResponse> {
  return useQuery({
    queryKey: queryKeys.sessions(id ?? ""),
    queryFn: () => api.getSessions(id!),
    enabled: Boolean(id),
    refetchInterval,
    refetchIntervalInBackground: true,
  });
}

// -- writes ----------------------------------------------------------------

/**
 * BEGIN, COMMIT and ROLLBACK as one hook.
 *
 * A rollback rewrites pages the rest of the UI is displaying — rows, the disk
 * map, the catalog, the page inspector — so every one of them is invalidated,
 * not just the transaction panel. Anything less would leave the explorer
 * showing rows that no longer exist, which is exactly the "fake frontend
 * simulation" this project refuses to ship.
 */
export function useTransactionAction(
  databaseId: string | null,
  session?: string,
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: "begin" | "commit" | "rollback") => {
      const id = databaseId!;
      if (action === "begin") return api.beginTransaction(id, session);
      if (action === "commit") return api.commitTransaction(id, session);
      return api.rollbackTransaction(id, session);
    },
    onSuccess: () => {
      if (databaseId) invalidateDatabase(client, databaseId);
    },
  });
}

export function useVacuum(databaseId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.vacuum(databaseId!),
    onSuccess: () => {
      if (databaseId) invalidateDatabase(client, databaseId);
    },
  });
}

export function useCheckpoint(databaseId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.checkpoint(databaseId!),
    onSuccess: () => {
      if (databaseId) invalidateDatabase(client, databaseId);
    },
  });
}

/**
 * The crash button. **Destroys uncommitted work.**
 *
 * Everything is invalidated afterwards, not just the log: recovery really did
 * roll pages back, so rows, the disk map, the catalog and the page inspector
 * are all showing state that no longer exists. Refreshing only the WAL panel
 * would leave the rest of the explorer confidently wrong, which is the failure
 * mode this project exists to avoid.
 */
export function useCrash(databaseId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.crash(databaseId!),
    onSuccess: () => {
      if (databaseId) invalidateDatabase(client, databaseId);
    },
  });
}

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
    mutationFn: (rows: unknown[][]) =>
      api.insertRecords(databaseId, table, rows),
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
export function useCreateIndex(databaseId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateIndexRequest) =>
      api.createIndex(databaseId, payload),
    // Building an index allocates pages and writes the catalog, so the storage
    // views are stale too — not just the index list.
    onSuccess: () => invalidateDatabase(client, databaseId),
  });
}

export function useParseSql(databaseId: string) {
  return useMutation({
    mutationFn: (sql: string) => api.parseSql(databaseId, sql),
  });
}

/**
 * Run SQL. A mutation, not a query: executing is an action with side effects
 * (INSERT writes pages), so it must never be re-fetched automatically.
 */
export function useRunQuery(databaseId: string, session?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ sql, maxRows }: { sql: string; maxRows?: number }) =>
      api.runQuery(databaseId, sql, maxRows, session),
    onSuccess: (results) => {
      // Only invalidate storage views when something was actually written.
      // A read-only SELECT changes nothing and should not cause a refetch storm.
      if (results.some((result) => result.rows_affected > 0)) {
        invalidateDatabase(client, databaseId);
      }
    },
    onError: () => {
      // A failed statement changes engine state too: it dooms the open
      // transaction, and it may have written rows before it raised. Leaving the
      // caches alone here would show a healthy transaction that is actually
      // refusing to accept anything.
      invalidateDatabase(client, databaseId);
    },
  });
}

export function useSetTraceLevel(databaseId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (level: TraceLevelName) => api.setTraceLevel(databaseId, level),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.trace(databaseId) });
      void client.invalidateQueries({
        queryKey: queryKeys.database(databaseId),
      });
    },
  });
}
