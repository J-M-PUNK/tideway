import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
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

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.playlists.mine();
      setPlaylists(list);
    } finally {
      setLoading(false);
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
