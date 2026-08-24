import { useCallback, useSyncExternalStore } from "react";

/**
 * Recent search queries, persisted per install.
 *
 * The search box updates results as you type, so there is no submit
 * event to hang "the user searched for this" off. What we record
 * instead is every debounced query that came back with results, then
 * collapse the typing stems on the way in: while the newest entry is a
 * strict prefix of the query being recorded, it gets dropped. Typing
 * "daft punk" with a pause after "daft" leaves one entry, not two,
 * and an unrelated older "daft" from last week is untouched because
 * stems are only ever consecutive.
 *
 * Storage is localStorage, which the shim in installStorageShim.ts
 * guarantees exists even when WebKitGTK takes the real one away. In
 * that degraded state history lives for the session and no longer.
 */
const STORAGE_KEY = "tideway:search-history";

/** Enough to cover "what was I just looking at" without turning the
 *  empty-search page into a wall of text. */
export const MAX_HISTORY = 10;

function read(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((v): v is string => typeof v === "string")
      .map((v) => v.trim())
      .filter(Boolean)
      .slice(0, MAX_HISTORY);
  } catch {
    // Never rethrow: this runs during render, so anything that
    // escapes takes the ErrorBoundary and blanks the app. Corrupt
    // JSON is one case. The other is storage itself going away —
    // installStorageShim only substitutes a working Storage at boot,
    // and the WebKitGTK web process it was written for can crash and
    // reload mid-session into a context with no storage binding, at
    // which point getItem throws. Losing the history is an acceptable
    // outcome there; losing the whole UI is not. Same shape as
    // ArtistSection's loadAlbumView.
    return [];
  }
}

function write(entries: string[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // Ten short strings can't fill a quota on their own, but they
    // share the origin with the Last.fm playcount cache, which holds
    // thousands of entries. If that has filled the bucket — or
    // storage has gone away entirely, as read() describes — losing
    // search history is the correct thing to lose, never the search.
  }
}

/** Same entries in the same order. Used to bail out of a no-op
 *  update so React skips the re-render and the persist effect. */
function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** Pure list transform, exported so the collapse rules can be tested
 *  without mounting a component. */
export function addToHistory(entries: string[], raw: string): string[] {
  const query = raw.trim();
  if (!query) return entries;

  const next = [...entries];
  const lower = query.toLowerCase();

  // Drop the stems this query was typed through. They sit at the head
  // of the list because they were recorded moments ago.
  while (next.length > 0) {
    const head = next[0].toLowerCase();
    if (head !== lower && lower.startsWith(head)) next.shift();
    else break;
  }

  // Case-insensitive dedupe: searching "Daft Punk" again moves the
  // existing entry to the top rather than adding a second spelling.
  const deduped = next.filter((e) => e.toLowerCase() !== lower);
  return [query, ...deduped].slice(0, MAX_HISTORY);
}

// One shared store rather than per-hook state. Two surfaces read this
// now — the Search page and the search bar's dropdown — and the
// dropdown is mounted the whole time the app is. With independent
// useState copies, a query recorded on the Search page would not
// reach the already-mounted dropdown until a remount, so the bar
// would keep offering a stale list.
//
// `cache` is read from storage once, lazily. useSyncExternalStore
// requires getSnapshot to return a stable reference for unchanged
// state, so re-reading storage on every call would loop forever.
let cache: string[] | null = null;
const subscribers = new Set<() => void>();

function getSnapshot(): string[] {
  if (cache === null) cache = read();
  return cache;
}

function subscribe(onChange: () => void): () => void {
  subscribers.add(onChange);
  return () => {
    subscribers.delete(onChange);
  };
}

/** Commit a new list: persist it, then wake every mounted reader.
 *  Only ever called from an event handler or an effect, never from a
 *  render or a state updater. */
function commit(next: string[]): void {
  if (sameOrder(next, getSnapshot())) return;
  cache = next;
  write(next);
  for (const notify of subscribers) notify();
}

/** Drop the in-memory copy so the next read comes from storage again.
 *  Exists for tests, which need each case to start from a known
 *  store; nothing in the app calls it. */
export function resetSearchHistoryCache(): void {
  cache = null;
}

export function useSearchHistory() {
  const history = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const record = useCallback((query: string) => {
    commit(addToHistory(getSnapshot(), query));
  }, []);

  const remove = useCallback((query: string) => {
    commit(getSnapshot().filter((e) => e !== query));
  }, []);

  const clear = useCallback(() => {
    commit([]);
  }, []);

  return { history, record, remove, clear };
}
