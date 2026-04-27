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
    let closed = false;
    let es: EventSource | null = null;
    let retryTimer: number | null = null;
    // Exponential backoff capped at 30s. EventSource auto-reconnects on
    // transport failure, but only while the server keeps replying 200 —
    // a 401 (session expired) or a long outage pushes the browser into
    // `readyState === CLOSED` and we have to rebuild the socket ourselves.
    let backoffMs = 1000;

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

    const connect = () => {
      if (closed) return;
      es = new EventSource("/api/downloads/stream");
      es.addEventListener("download", handle as EventListener);
      es.onopen = () => {
        // Successful handshake — reset the backoff so the next failure
        // starts from 1s again.
        backoffMs = 1000;
      };
      es.onerror = () => {
        if (closed) return;
        // CLOSED means the browser gave up reconnecting. Tear down and
        // schedule our own reconnect with backoff.
        if (es && es.readyState === EventSource.CLOSED) {
          es.close();
          es = null;
          retryTimer = window.setTimeout(connect, backoffMs);
          backoffMs = Math.min(backoffMs * 2, 30000);
        }
      };
    };
    connect();

    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      if (es) {
        es.removeEventListener("download", handle as EventListener);
        es.close();
      }
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
  if (!ctx)
    throw new Error(
      "useDownloadStream must be inside <DownloadStreamProvider>",
    );
  return ctx;
}
