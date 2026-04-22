import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/api/client";
import type { VideoDownloadJob } from "@/api/types";

/**
 * Shared state for video-download jobs. Polled by the provider once
 * and fanned out to every subscriber, so the sidebar badge and the
 * Downloads page pull from the same source of truth without each
 * maintaining its own polling loop.
 *
 * Cadence: 500 ms while anything is running (progress feels live),
 * 10 s when everything is idle (sidebar badge update latency). The
 * sidebar mounts for the entire app lifetime, so we want the idle
 * cost to be near-zero — a 10 s tick is fine.
 */

interface Ctx {
  jobs: VideoDownloadJob[];
  active: VideoDownloadJob[];
  terminal: VideoDownloadJob[];
}

const VideoDownloadsCtx = createContext<Ctx>({
  jobs: [],
  active: [],
  terminal: [],
});

export function VideoDownloadsProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<VideoDownloadJob[]>([]);
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    const tick = async () => {
      if (cancelled) return;
      try {
        const list = await api.videoDownloadsList();
        if (!cancelled) setJobs(list);
        const hasActive = list.some((j) => j.state === "running");
        timer = window.setTimeout(tick, hasActive ? 500 : 10000);
      } catch {
        // Transient — back off and retry. Not surfaced in the UI
        // because a dead endpoint just means an empty list, which
        // is semantically equivalent to "no video downloads".
        timer = window.setTimeout(tick, 10000);
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  const value = useMemo<Ctx>(() => {
    const active = jobs.filter((j) => j.state === "running");
    const terminal = jobs.filter(
      (j) => j.state === "done" || j.state === "error",
    );
    return { jobs, active, terminal };
  }, [jobs]);

  return (
    <VideoDownloadsCtx.Provider value={value}>
      {children}
    </VideoDownloadsCtx.Provider>
  );
}

export function useVideoDownloads(): Ctx {
  return useContext(VideoDownloadsCtx);
}
