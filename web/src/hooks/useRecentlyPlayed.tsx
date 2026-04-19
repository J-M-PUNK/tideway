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

const STORAGE_KEY = "tidal-downloader:recents";
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

  const value = useMemo(() => ({ tracks, record, clear }), [tracks, record, clear]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRecentlyPlayed() {
  return useContext(Ctx);
}

/**
 * Record a track into the recently-played list once it's been listened to
 * for ~10 seconds. Single effect keyed on (trackId, currentTime) with a
 * ref guarding "did I already log this instance of this track?" — prevents
 * the double-effect race where a reset effect fires after a record effect.
 */
export function useRecordPlays(track: Track | null, currentTime: number): void {
  const { record } = useRecentlyPlayed();
  const lastRecordedId = useRef<string | null>(null);

  useEffect(() => {
    if (!track) return;
    if (lastRecordedId.current === track.id) return;
    if (currentTime < 10) return;
    record(track);
    lastRecordedId.current = track.id;
  }, [track, currentTime, record]);
}
