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
import { api } from "@/api/client";
import type { Playlist } from "@/api/types";

interface MyPlaylistsContextValue {
  playlists: Playlist[];
  loading: boolean;
  refresh: () => Promise<void>;
  /** Optimistically prepend a newly-created playlist. */
  optimisticAdd: (p: Playlist) => void;
  /** Optimistically drop a deleted playlist. */
  optimisticRemove: (id: string) => void;
}

const Ctx = createContext<MyPlaylistsContextValue>({
  playlists: [],
  loading: false,
  refresh: async () => {},
  optimisticAdd: () => {},
  optimisticRemove: () => {},
});

/**
 * Caches the user's own playlists. Shared via context because multiple
 * consumers need the list (sidebar, add-to-playlist menu, playlist detail)
 * and we'd rather not fetch more than once.
 */
export function MyPlaylistsProvider({ children }: { children: ReactNode }) {
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [loading, setLoading] = useState(true);

  // Monotonic token used to discard stale refresh responses. Two nearly-
  // simultaneous refresh() calls (e.g. Create + Edit dialogs firing in
  // the same tick) would otherwise race: whichever response lands LAST
  // wins, regardless of which request is actually the newer one.
  const refreshToken = useRef(0);

  const refresh = useCallback(async () => {
    const token = ++refreshToken.current;
    setLoading(true);
    try {
      const list = await api.playlists.mine();
      if (token !== refreshToken.current) return;
      setPlaylists(list);
    } finally {
      if (token === refreshToken.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh().catch(() => setLoading(false));
  }, [refresh]);

  const optimisticAdd = useCallback((p: Playlist) => {
    setPlaylists((prev) => [p, ...prev.filter((x) => x.id !== p.id)]);
  }, []);

  const optimisticRemove = useCallback((id: string) => {
    setPlaylists((prev) => prev.filter((x) => x.id !== id));
  }, []);

  const value = useMemo(
    () => ({ playlists, loading, refresh, optimisticAdd, optimisticRemove }),
    [playlists, loading, refresh, optimisticAdd, optimisticRemove],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useMyPlaylists() {
  return useContext(Ctx);
}
