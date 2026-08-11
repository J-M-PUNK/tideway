import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";
import { useLocation, useNavigationType } from "react-router-dom";

// Keys that scroll the container. A plain keydown listener would also
// fire while the user types into a search box, which would abandon a
// restore that was still in progress.
const SCROLL_KEYS = new Set([
  "PageUp",
  "PageDown",
  "Home",
  "End",
  "ArrowUp",
  "ArrowDown",
  " ",
]);

/**
 * Per-history-entry scroll restoration for the app's custom scroll
 * container (#315).
 *
 * The routed view scrolls inside an `overflow-y-auto` <main>, not the
 * window, so the browser never restores scroll on its own. Originally we
 * hard-reset to the top on every navigation, so scrolling deep into
 * Artists/Albums/Feed, opening an item, and coming back always dumped you
 * at the top.
 *
 * Behaviour:
 *   - PUSH / REPLACE (following a link) → start at the top.
 *   - POP (back / forward) → restore where you were.
 *
 * Restoring lives in one layout effect. Its setup restores or resets for
 * the entry being entered; its cleanup records the offset for the entry
 * being left, keyed on `location.key`, which is unique per history entry.
 *
 * Two things make this harder than it looks, and the first is what
 * reopened the issue.
 *
 * Recording the offset cannot just read `scrollTop` on the way out. By
 * the time any layout effect runs, React has already swapped the outgoing
 * route's content out during the render phase. The container is suddenly
 * far shorter, often just a Suspense skeleton, and the browser has
 * clamped `scrollTop` down to fit it. Reading it at cleanup returns that
 * clamped remnant instead of where the user actually was: leaving a
 * library list at 8000px recorded about 140px, so coming back landed near
 * the top. Scroll events are dispatched asynchronously, after the commit,
 * so the offset tracked by the listener below has not yet been overwritten
 * by the clamp and still holds the real position.
 *
 * Applying the offset cannot happen in a single frame either, because a
 * page's content arrives after mount and `scrollTop` clamps to whatever
 * the container can currently scroll. So we re-apply as the content grows,
 * watching the children with a ResizeObserver, since the container's own
 * box does not change when its content does but `scrollHeight` does. That
 * is event-driven with no deadline, which matters twice over: this was a
 * rAF loop bounded by a 1000ms grace window, and any list slower than a
 * second to come back from Tidal landed short, while an unbounded rAF loop
 * would be the idle spin that #308 was about. When content stops growing,
 * the observer stops firing.
 *
 * Restoring stops as soon as the offset is reachable, or as soon as the
 * user scrolls for themselves, so late-arriving content cannot yank them
 * back to a position they had already chosen to leave.
 */
export function useScrollRestoration(ref: RefObject<HTMLElement | null>): void {
  const location = useLocation();
  const navType = useNavigationType();
  const positions = useRef<Map<string, number>>(new Map());
  // The live scroll offset. See above on why reading scrollTop at
  // navigation time is already too late.
  const lastTop = useRef(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      lastTop.current = el.scrollTop;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [ref]);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const key = location.key;
    const target = navType === "POP" ? (positions.current.get(key) ?? 0) : 0;

    if (target <= 0) {
      el.scrollTo({ top: 0 });
      lastTop.current = 0;
      return () => positions.current.set(key, lastTop.current);
    }

    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      resize.disconnect();
      children.disconnect();
      el.removeEventListener("wheel", onTakeover);
      el.removeEventListener("touchstart", onTakeover);
      window.removeEventListener("keydown", onKeyTakeover);
    };

    const apply = () => {
      if (settled) return;
      const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
      el.scrollTop = Math.min(target, maxTop);
      // Once the container can hold the offset there is nothing left to
      // wait for; further growth below the fold is not our business.
      if (maxTop >= target) {
        lastTop.current = target;
        settle();
      }
    };

    const onTakeover = () => settle();
    const onKeyTakeover = (e: KeyboardEvent) => {
      if (SCROLL_KEYS.has(e.key)) settle();
    };

    const resize = new ResizeObserver(apply);
    const observeChildren = () => {
      for (const child of Array.from(el.children)) resize.observe(child);
    };
    // Banners mount and unmount, so the set of children is not fixed.
    const children = new MutationObserver(() => {
      observeChildren();
      apply();
    });

    observeChildren();
    children.observe(el, { childList: true });
    el.addEventListener("wheel", onTakeover, { passive: true });
    el.addEventListener("touchstart", onTakeover, { passive: true });
    window.addEventListener("keydown", onKeyTakeover);
    apply();

    return () => {
      const reached = settled;
      settle();
      // Leaving before the content ever grew enough to hold the offset
      // means the tracked value is a partial position we were clamped
      // to. Keep aiming at the original target so a later return still
      // lands in the right place.
      positions.current.set(key, reached ? lastTop.current : target);
    };
  }, [ref, location.key, navType]);
}
