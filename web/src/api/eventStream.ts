/**
 * The app's one server-sent-event connection (GET /api/events). Player
 * snapshots, download queue events and global hotkey actions arrive on
 * it as the named events `player`, `download` and `hotkey`.
 *
 * These used to be three EventSources, and each held one of the six
 * HTTP/1.1 connections Chromium allows per host for as long as the app
 * was open. `req()` caps API calls at four on the assumption that six
 * are available, so with three taken permanently, any three API calls
 * in flight left nothing for cover images or route chunks, which then
 * queued for seconds in the browser behind enrichment requests.
 *
 * The connection opens with the first subscriber and closes with the
 * last. Every (re)connect starts with a full snapshot from each source,
 * so a subscriber never has to resync after a reconnect.
 */

type Handler = (data: string) => void;

const handlers = new Map<string, Set<Handler>>();
// The listener registered on the current EventSource for each event
// name, so it can be removed when the name's last handler goes.
const attached = new Map<string, EventListener>();

let source: EventSource | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
// The browser reconnects on its own while the server keeps answering
// 200. A non-200 (a 401 after the session expired, the backend not up
// yet) leaves the EventSource CLOSED and it's on us to rebuild it.
let backoffMs = 1000;
const MAX_BACKOFF_MS = 5000;

function dispatch(name: string, event: Event): void {
  const data = (event as MessageEvent).data;
  if (typeof data !== "string") return;
  // Copy so a handler that unsubscribes during dispatch doesn't
  // perturb the iteration.
  for (const fn of Array.from(handlers.get(name) ?? [])) {
    try {
      fn(data);
    } catch {
      /* one subscriber's bug shouldn't take down the others */
    }
  }
}

function attach(name: string): void {
  if (!source || attached.has(name)) return;
  const listener: EventListener = (event) => dispatch(name, event);
  source.addEventListener(name, listener);
  attached.set(name, listener);
}

function open(): void {
  retryTimer = null;
  const es = new EventSource("/api/events");
  source = es;
  attached.clear();
  for (const name of handlers.keys()) attach(name);
  es.onopen = () => {
    backoffMs = 1000;
  };
  es.onerror = () => {
    if (source !== es || es.readyState !== EventSource.CLOSED) return;
    es.close();
    source = null;
    attached.clear();
    if (handlers.size === 0) return;
    retryTimer = setTimeout(open, backoffMs);
    backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
  };
}

function close(): void {
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  if (source) {
    source.close();
    source = null;
  }
  attached.clear();
  backoffMs = 1000;
}

/** Call `handler` with the raw `data` of every `name` event on the
 *  app's event stream. Returns the unsubscribe function. */
export function subscribeEvents(name: string, handler: Handler): () => void {
  let set = handlers.get(name);
  if (!set) {
    set = new Set();
    handlers.set(name, set);
  }
  set.add(handler);
  if (source) attach(name);
  else if (retryTimer === null) open();

  return () => {
    const current = handlers.get(name);
    if (!current) return;
    current.delete(handler);
    if (current.size > 0) return;
    handlers.delete(name);
    const listener = attached.get(name);
    if (listener && source) source.removeEventListener(name, listener);
    attached.delete(name);
    if (handlers.size === 0) close();
  };
}
