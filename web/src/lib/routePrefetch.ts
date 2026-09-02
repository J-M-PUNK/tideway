import { apiRequestsInFlight } from "@/api/client";

/**
 * Warms lazily-loaded route chunks while the connection pool is idle.
 *
 * Route components are separate chunks, so navigating to a page the
 * user hasn't visited needs a network round trip at click time. That
 * round trip competes with API calls for the browser's six connections
 * to 127.0.0.1, and during startup the API calls win — the click
 * registers, the route changes, and Suspense shows a skeleton until a
 * connection frees up.
 *
 * Warming the chunks ahead of time removes the round trip entirely, so
 * navigation stops depending on how busy the API is.
 *
 * The timing is the whole point. Prefetching eagerly would make the
 * problem worse: thirty-odd chunk requests during startup would take
 * the very connections the first page's data needs. So we only warm
 * while the pool is quiet, one chunk at a time, and back off the moment
 * real requests appear.
 */

const factories: Array<() => Promise<unknown>> = [];
const warmed = new Set<() => Promise<unknown>>();

/** Register a route's import so it can be prefetched later. Called by
 *  `lazyRoute` in App.tsx; order is the order routes are declared. */
export function registerRouteChunk(factory: () => Promise<unknown>): void {
  factories.push(factory);
}

/** Warm one chunk immediately — for hover, where the user has told us
 *  where they are going and waiting for idle would be too late. */
export function prefetchRouteChunk(factory: () => Promise<unknown>): void {
  if (warmed.has(factory)) return;
  warmed.add(factory);
  void factory().catch(() => {
    // A failed prefetch is not a failure: the route will import again
    // on navigation and surface any real error there.
    warmed.delete(factory);
  });
}

const onIdle = (fn: () => void, timeout: number): void => {
  const ric = (
    globalThis as { requestIdleCallback?: (cb: () => void, o?: object) => void }
  ).requestIdleCallback;
  if (typeof ric === "function") ric(fn, { timeout });
  else setTimeout(fn, timeout);
};

/**
 * Start warming route chunks once the app settles.
 *
 * Returns a stop function. Safe to call more than once; a second call
 * is a no-op while the first is still running.
 */
let running = false;
export function startRoutePrefetch(): () => void {
  if (running) return () => {};
  running = true;
  let stopped = false;

  const step = () => {
    if (stopped) return;
    // Only while nothing else needs the pool. Re-checking every tick
    // rather than once up front matters: the user can navigate at any
    // point, and their page's data must not queue behind a prefetch.
    if (apiRequestsInFlight() > 0) {
      onIdle(step, 2000);
      return;
    }
    const next = factories.find((f) => !warmed.has(f));
    if (!next) {
      running = false;
      return;
    }
    warmed.add(next);
    void next()
      .catch(() => warmed.delete(next))
      .finally(() => onIdle(step, 500));
  };

  // Deliberately late. The first screen's data matters more than any
  // chunk, and it is exactly the window where connections are scarce.
  onIdle(step, 4000);

  return () => {
    stopped = true;
    running = false;
  };
}
