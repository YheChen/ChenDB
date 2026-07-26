import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ExplorerPage } from "@/pages/ExplorerPage";

/**
 * Engine state is short-lived by nature: inserting a row changes the page list,
 * the row count and the file size at once. Data is therefore treated as stale
 * immediately, and mutations invalidate precisely what they touched.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 0,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ErrorBoundary label="ChenDB Explorer">
        <ExplorerPage />
      </ErrorBoundary>
    </QueryClientProvider>
  );
}
