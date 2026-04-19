import { useEffect, useState } from "react";

interface State<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
}

/**
 * Minimal data-fetching hook. Runs `fetcher` on mount and whenever `deps`
 * change; tracks loading/error/data. Cancels stale requests on unmount or
 * dep change so late responses can't stomp fresh state.
 *
 * Intentionally tiny — a full cache layer (SWR/Query) would be overkill for
 * a single-user local tool.
 */
export function useApi<T>(fetcher: () => Promise<T>, deps: React.DependencyList = []): State<T> {
  const [state, setState] = useState<State<T>>({ data: null, loading: true, error: null });

  useEffect(() => {
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));
    fetcher()
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({
            data: null,
            loading: false,
            error: err instanceof Error ? err : new Error(String(err)),
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
