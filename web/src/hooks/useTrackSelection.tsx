import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";
import type { Track } from "@/api/types";

interface SelectionContextValue {
  /** Map<trackId, Track> — preserves the Track object so action handlers
   *  can read name/artists without round-tripping via the server. */
  selected: Map<string, Track>;
  has: (trackId: string) => boolean;
  toggle: (track: Track) => void;
  /** Select a range defined by two anchor IDs inside `from`. Used by
   *  shift-clicking to select everything between two checkbox clicks. */
  toggleRange: (from: Track[], fromId: string, toId: string) => void;
  /** Clear only the IDs named. Bulk actions use this so tracks selected
   *  *during* an in-flight action aren't dropped when the action resolves. */
  removeMany: (ids: string[]) => void;
  clear: () => void;
}

const EMPTY_MAP = new Map<string, Track>();

const Ctx = createContext<SelectionContextValue>({
  selected: EMPTY_MAP,
  has: () => false,
  toggle: () => {},
  toggleRange: () => {},
  removeMany: () => {},
  clear: () => {},
});

/**
 * Maintains a cross-page set of "selected" tracks. Checkboxes on TrackRow
 * call `toggle`; the floating action bar reads `selected` to present
 * Download / Like / Add-to-Playlist / Clear. Selection clears on route
 * change so you don't accidentally act on stale selections you made
 * somewhere else.
 */
export function TrackSelectionProvider({ children }: { children: ReactNode }) {
  const [selected, setSelected] = useState<Map<string, Track>>(EMPTY_MAP);
  const location = useLocation();

  // Clear on route change (but not on the initial mount).
  useEffect(() => {
    setSelected(EMPTY_MAP);
  }, [location.pathname]);

  const toggle = useCallback((track: Track) => {
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(track.id)) next.delete(track.id);
      else next.set(track.id, track);
      return next;
    });
  }, []);

  const toggleRange = useCallback(
    (list: Track[], fromId: string, toId: string) => {
      const a = list.findIndex((t) => t.id === fromId);
      const b = list.findIndex((t) => t.id === toId);
      if (a < 0 || b < 0) return;
      const lo = Math.min(a, b);
      const hi = Math.max(a, b);
      setSelected((prev) => {
        const next = new Map(prev);
        for (let i = lo; i <= hi; i++) {
          const t = list[i];
          if (t) next.set(t.id, t);
        }
        return next;
      });
    },
    [],
  );

  const clear = useCallback(() => setSelected(EMPTY_MAP), []);

  const removeMany = useCallback((ids: string[]) => {
    setSelected((prev) => {
      const next = new Map(prev);
      ids.forEach((id) => next.delete(id));
      return next;
    });
  }, []);

  const has = useCallback(
    (trackId: string) => selected.has(trackId),
    [selected],
  );

  const value = useMemo(
    () => ({ selected, has, toggle, toggleRange, removeMany, clear }),
    [selected, has, toggle, toggleRange, removeMany, clear],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTrackSelection() {
  return useContext(Ctx);
}
