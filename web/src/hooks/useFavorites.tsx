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
  mix: new Set(),
};

const Ctx = createContext<FavoritesContextValue>({
  has: () => false,
  toggle: async () => {},
});

/**
 * Tracks which Tidal entities the user has favorited. Hydrated from
 * /api/favorites on mount; mutations are optimistic and roll back on
 * server error.
 *
 * Re-hydrates whenever the tab becomes visible again so library changes
 * made on another Tidal client (mobile/web/desktop) propagate without
 * needing a manual reload. The snapshot endpoint is moderately
 * expensive (it page-scrapes each kind's favorites), but tab-visibility
 * fires only on real focus events, not while the user is actively using
 * the app, so the cost is bounded.
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

  // `mutatedBeforeSnapshot` guards against the very first snapshot
  // clobbering an optimistic toggle the user fired before it resolved.
  // Subsequent refetches use `inFlightMutations` instead.
  const mutatedBeforeSnapshot = useRef(false);
  // Counter of in-flight POST/DELETE /api/favorites calls. A refetch
  // that lands while a mutation is racing would either re-add a just-
  // unhearted track or drop a just-hearted one, so we skip refetches
  // while any mutation is pending and let the next visibility event
  // catch up.
  const inFlightMutations = useRef(0);
  // Latch for the first fetch so the initial-mount path runs the
  // merge-if-needed logic but later visibility-driven refetches just
  // replace.
  const initialFetchDone = useRef(false);

  useEffect(() => {
    let cancelled = false;

    const refetch = () => {
      if (cancelled) return;
      if (initialFetchDone.current && inFlightMutations.current > 0) {
        // Mutation in flight — skip this round; next focus catches it.
        return;
      }
      api.favorites
        .snapshot()
        .then((snap) => {
          if (cancelled) return;
          const isInitial = !initialFetchDone.current;
          initialFetchDone.current = true;
          if (isInitial && mutatedBeforeSnapshot.current) {
            // Initial fetch raced an early click. Union so the user's
            // optimistic adds aren't dropped; server is authoritative
            // for entries the user didn't touch.
            setSets((prev) => ({
              track: mergeSets(new Set(snap.tracks), prev.track),
              album: mergeSets(new Set(snap.albums), prev.album),
              artist: mergeSets(new Set(snap.artists), prev.artist),
              playlist: mergeSets(new Set(snap.playlists), prev.playlist),
              mix: mergeSets(new Set(snap.mixes), prev.mix),
            }));
            return;
          }
          setSets({
            track: new Set(snap.tracks),
            album: new Set(snap.albums),
            artist: new Set(snap.artists),
            playlist: new Set(snap.playlists),
            mix: new Set(snap.mixes),
          });
        })
        .catch(() => {
          /* non-critical — render unfilled hearts */
        });
    };

    refetch();

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        refetch();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
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
      mutatedBeforeSnapshot.current = true;
      inFlightMutations.current += 1;
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
        // Notify other surfaces that depend on the full library/tracks
        // payload (e.g. `useLikedTracksByArtist` on the artist page's
        // "Liked songs" section) so they can refetch. The event is
        // a low-cost broadcast; only listeners on a current artist
        // page actually do anything with it.
        window.dispatchEvent(new CustomEvent("tideway:favorite-toggled"));
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
      } finally {
        inFlightMutations.current -= 1;
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

// Union the two sets — used when a late-arriving snapshot must not drop
// the user's optimistic additions. Removals during the pre-snapshot
// window are lost, which is acceptable: re-firing a remove is cheaper
// than silently re-adding a track the user just explicitly unhearted.
function mergeSets(server: Set<string>, optimistic: Set<string>): Set<string> {
  const out = new Set(server);
  optimistic.forEach((id) => out.add(id));
  return out;
}
