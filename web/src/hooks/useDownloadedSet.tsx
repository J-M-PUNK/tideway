import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "@/api/client";
import { useDownloadStream } from "./useDownloadStream";

interface DownloadedContextValue {
  has: (trackId: string) => boolean;
}

const Ctx = createContext<DownloadedContextValue>({ has: () => false });

/**
 * Tracks which Tidal track IDs we have as local files. Hydrated once from
 * /api/downloaded, then updated live via the shared SSE broker's
 * `downloaded` event (the backend emits when a download completes).
 *
 * Shared via context because every track row needs it — fetching per-list
 * would be wasteful, and the dataset is small (a couple thousand strings).
 */
export function DownloadedProvider({ children }: { children: ReactNode }) {
  const stream = useDownloadStream();
  const [ids, setIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    api
      .downloaded()
      .then(({ ids }) => {
        if (!cancelled) setIds(new Set(ids));
      })
      .catch(() => {
        /* leave empty */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    return stream.subscribe((payload) => {
      if (payload.type !== "downloaded") return;
      const tid = payload.track_id;
      if (typeof tid !== "string") return;
      setIds((prev) => {
        if (prev.has(tid)) return prev;
        const next = new Set(prev);
        next.add(tid);
        return next;
      });
    });
  }, [stream]);

  const value = useMemo<DownloadedContextValue>(
    () => ({ has: (trackId: string) => ids.has(String(trackId)) }),
    [ids],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useIsDownloaded(trackId: string): boolean {
  return useContext(Ctx).has(trackId);
}
