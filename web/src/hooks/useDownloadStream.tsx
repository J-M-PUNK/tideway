import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { subscribeEvents } from "@/api/eventStream";

export type DownloadEvent = { type: string; [key: string]: unknown };
type Listener = (evt: DownloadEvent) => void;

interface StreamContextValue {
  subscribe: (listener: Listener) => () => void;
}

const Ctx = createContext<StreamContextValue | null>(null);

/**
 * The download queue's events, from the app's shared event stream
 * (see `@/api/eventStream`), fanned out to every consumer.
 *
 * Both `useDownloads` and `useDownloadedSet` listen; they subscribe here
 * and filter for the event types they care about. After any reconnect
 * the first event is a `reset` snapshot of the whole queue.
 *
 * `subscribe` and the context value are stable so consumers including
 * `subscribe` in their effect deps don't thrash listeners on every render.
 */
export function DownloadStreamProvider({ children }: { children: ReactNode }) {
  const listenersRef = useRef<Set<Listener>>(new Set());

  useEffect(
    () =>
      subscribeEvents("download", (data) => {
        let payload: DownloadEvent;
        try {
          payload = JSON.parse(data) as DownloadEvent;
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
      }),
    [],
  );

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
