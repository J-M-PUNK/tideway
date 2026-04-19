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
import type { FavoriteKind } from "@/api/types";
import { useToast } from "@/components/toast";

type Sets = Record<FavoriteKind, Set<string>>;

interface FavoritesContextValue {
  has: (kind: FavoriteKind, id: string) => boolean;
  toggle: (kind: FavoriteKind, id: string) => Promise<void>;
}

const EMPTY: Sets = {
  track: new Set(),
  album: new Set(),
  artist: new Set(),
  playlist: new Set(),
};

const Ctx = createContext<FavoritesContextValue>({
  has: () => false,
  toggle: async () => {},
});

/**
 * Tracks which Tidal entities the user has favorited. Hydrated once from
 * /api/favorites; mutations are optimistic and roll back on server error.
 *
 * The snapshot endpoint is moderately expensive (it page-scrapes each kind's
 * favorites), so we only call it once per session.
 */
export function FavoritesProvider({ children }: { children: ReactNode }) {
  const [sets, setSets] = useState<Sets>(EMPTY);
  const toast = useToast();
  // Ref mirror so `toggle` can read the current sets without depending on
  // them — otherwise every mutation re-creates `toggle`, which re-creates
  // the context value, which re-renders every heart button on the page.
  const setsRef = useRef(sets);
  useEffect(() => {
    setsRef.current = sets;
  }, [sets]);

  useEffect(() => {
    let cancelled = false;
    api.favorites
      .snapshot()
      .then((snap) => {
        if (cancelled) return;
        setSets({
          track: new Set(snap.tracks),
          album: new Set(snap.albums),
          artist: new Set(snap.artists),
          playlist: new Set(snap.playlists),
        });
      })
      .catch(() => {
        /* non-critical — render unfilled hearts */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // `has` intentionally depends on `sets` — consumers should re-render when
  // their item's liked state flips. The expensive callbacks on the other
  // hand stay stable.
  const has = useCallback(
    (kind: FavoriteKind, id: string) => sets[kind].has(String(id)),
    [sets],
  );

  const toggle = useCallback(
    async (kind: FavoriteKind, id: string) => {
      const already = setsRef.current[kind].has(id);
      const apply = (add: boolean) =>
        setSets((prev) => {
          const next = { ...prev, [kind]: new Set(prev[kind]) };
          if (add) next[kind].add(id);
          else next[kind].delete(id);
          return next;
        });
      apply(!already);
      try {
        if (already) await api.favorites.remove(kind, id);
        else await api.favorites.add(kind, id);
      } catch (err) {
        // Only roll back if the current state still reflects *our* optimistic
        // change. A rapid second click may have toggled again between the
        // optimistic update and this error; blindly restoring `already`
        // would overwrite the user's latest intent.
        const current = setsRef.current[kind].has(id);
        if (current === !already) {
          apply(already);
        }
        toast.show({
          kind: "error",
          title: already ? "Couldn't unlike" : "Couldn't like",
          description: err instanceof Error ? err.message : String(err),
        });
      }
    },
    [toast],
  );

  const value = useMemo(() => ({ has, toggle }), [has, toggle]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useFavorites() {
  return useContext(Ctx);
}
