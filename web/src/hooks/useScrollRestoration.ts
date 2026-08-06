import { useLayoutEffect, useRef, type RefObject } from "react";
import { useLocation, useNavigationType } from "react-router-dom";

/**
 * Per-history-entry scroll restoration for the app's custom scroll
 * container (#315).
 *
 * The routed view scrolls inside an `overflow-y-auto` <main>, not the
 * window, so the browser never restores scroll on its own. Previously we
 * hard-reset to the top on every navigation — so scrolling deep into
 * Artists/Albums/Feed, opening an item, and coming back always dumped you
 * at the top.
 *
 * Behaviour now:
 *   - PUSH / REPLACE (following a link) → start at the top.
 *   - POP (back / forward) → restore where you were.
 *
 * Everything lives in one layout effect. Its setup restores/resets for the
 * entry being entered; its cleanup snapshots the current offset for the
 * entry being left. Ordering matters: on navigation React runs the
 * outgoing entry's cleanup *before* the incoming entry's setup, so we
 * capture the real scroll position before the shared container is reset —
 * a two-effect version raced and saved a zeroed value. Positions are keyed
 * on `location.key` (unique per history entry) and re-applied as async page
 * content grows, so a list that streams in after mount still lands where it
 * was instead of clamping short.
 */
export function useScrollRestoration(ref: RefObject<HTMLElement | null>): void {
  const location = useLocation();
  const navType = useNavigationType();
  const positions = useRef<Map<string, number>>(new Map());

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const key = location.key;
    let raf = 0;

    const target = navType === "POP" ? (positions.current.get(key) ?? 0) : 0;
    if (target <= 0) {
      el.scrollTo({ top: 0 });
    } else {
      // Async pages grow after mount, so the target may be unreachable on
      // the first frame. Nudge toward it each frame until the content is
      // tall enough to hold it, or a short grace window elapses.
      const startedAt = performance.now();
      const apply = () => {
        const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
        el.scrollTop = Math.min(target, maxTop);
        if (maxTop >= target || performance.now() - startedAt > 1000) return;
        raf = requestAnimationFrame(apply);
      };
      raf = requestAnimationFrame(apply);
    }

    return () => {
      if (raf) cancelAnimationFrame(raf);
      positions.current.set(key, el.scrollTop);
    };
  }, [ref, location.key, navType]);
}
