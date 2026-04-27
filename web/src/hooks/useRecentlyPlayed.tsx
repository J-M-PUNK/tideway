import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { Track } from "@/api/types";

const STORAGE_KEY = "tideway:recents";
const MAX_ENTRIES = 30;

interface RecentsContextValue {
  tracks: Track[];
  record: (track: Track) => void;
  clear: () => void;
}

const Ctx = createContext<RecentsContextValue>({
  tracks: [],
  record: () => {},
  clear: () => {},
});

function load(): Track[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function RecentsProvider({ children }: { children: ReactNode }) {
  const [tracks, setTracks] = useState<Track[]>(() => load());

  const record = useCallback((track: Track) => {
    setTracks((prev) => {
      const filtered = prev.filter((t) => t.id !== track.id);
      const next = [track, ...filtered].slice(0, MAX_ENTRIES);
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setTracks([]);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }, []);

  const value = useMemo(
    () => ({ tracks, record, clear }),
    [tracks, record, clear],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRecentlyPlayed() {
  return useContext(Ctx);
}

/**
 * Record a track into the recently-played list once it's been listened to
 * for ~10 seconds. The effect depends on a coarsened `reached10s` boolean
 * rather than raw `currentTime` so it doesn't churn four times per second
 * — two re-renders per track is enough (track change + threshold crossing).
 */
export function useRecordPlays(track: Track | null, currentTime: number): void {
  const { record } = useRecentlyPlayed();
  const lastRecordedId = useRef<string | null>(null);
  const reached10s = currentTime >= 10;

  useEffect(() => {
    if (!track || !reached10s) return;
    if (lastRecordedId.current === track.id) return;
    record(track);
    lastRecordedId.current = track.id;
  }, [track, reached10s, record]);
}
