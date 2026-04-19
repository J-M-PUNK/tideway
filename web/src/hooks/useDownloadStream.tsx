import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";

export type DownloadEvent = { type: string; [key: string]: unknown };
type Listener = (evt: DownloadEvent) => void;

interface StreamContextValue {
  subscribe: (listener: Listener) => () => void;
}

const Ctx = createContext<StreamContextValue | null>(null);

/**
 * One EventSource connection to /api/downloads/stream for the whole app.
 *
 * Before this existed, both `useDownloads` and `useDownloadedSet` opened
 * independent EventSources, doubling server load and eating into the
 * browser's per-host HTTP/1.1 connection limit. Consumers subscribe to this
 * single broker and filter for the event types they care about.
 *
 * `subscribe` and the context value are stable so consumers including
 * `subscribe` in their effect deps don't thrash listeners on every render.
 */
export function DownloadStreamProvider({ children }: { children: ReactNode }) {
  const listenersRef = useRef<Set<Listener>>(new Set());

  useEffect(() => {
    const es = new EventSource("/api/downloads/stream");
    const handle = (evt: MessageEvent) => {
      let payload: DownloadEvent;
      try {
        payload = JSON.parse(evt.data) as DownloadEvent;
      } catch {
        return;
      }
      if (!payload || typeof payload !== "object") return;
      // Snapshot so a listener that unsubscribes during dispatch doesn't
      // perturb the iteration.
      for (const fn of Array.from(listenersRef.current)) {
        try {
          fn(payload);
        } catch {
          /* one listener's bug shouldn't take down the others */
        }
      }
    };
    es.addEventListener("download", handle as EventListener);
    return () => {
      es.removeEventListener("download", handle as EventListener);
      es.close();
    };
  }, []);

  const subscribe = useCallback<StreamContextValue["subscribe"]>((listener) => {
    listenersRef.current.add(listener);
    return () => {
      listenersRef.current.delete(listener);
    };
  }, []);

  const value = useMemo<StreamContextValue>(() => ({ subscribe }), [subscribe]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDownloadStream(): StreamContextValue {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useDownloadStream must be inside <DownloadStreamProvider>");
  return ctx;
}
