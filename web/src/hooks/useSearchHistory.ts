import { useCallback, useEffect, useRef, useState } from "react";

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

export function useSearchHistory() {
  // Read in the initializer, not an effect: installStorageShim runs
  // before the first render, so storage is already usable here, and
  // seeding state directly avoids a second render pass on every mount
  // of the search page.
  const [history, setHistory] = useState<string[]>(read);

  // Persisting happens here rather than inside the state updaters,
  // because an updater has to be pure: React may run one
  // speculatively and discard the result, which would leave
  // localStorage holding a list that never became state. StrictMode
  // also double-invokes them in development.
  //
  // The mount pass is skipped deliberately. `history` starts as
  // whatever read() returned, and read() falls back to [] when
  // storage is unreadable — writing that back immediately would turn
  // a transient read failure into permanent data loss.
  const loaded = useRef(false);
  useEffect(() => {
    if (!loaded.current) {
      loaded.current = true;
      return;
    }
    write(history);
  }, [history]);

  const record = useCallback((query: string) => {
    // Returning `prev` unchanged when nothing moved keeps React from
    // re-rendering and keeps the persist effect from firing. The
    // search page calls this again on every re-fetch of the same
    // query, so the no-op case is the common one.
    setHistory((prev) => {
      const next = addToHistory(prev, query);
      return sameOrder(next, prev) ? prev : next;
    });
  }, []);

  const remove = useCallback((query: string) => {
    setHistory((prev) => {
      const next = prev.filter((e) => e !== query);
      return sameOrder(next, prev) ? prev : next;
    });
  }, []);

  const clear = useCallback(() => {
    setHistory((prev) => (prev.length === 0 ? prev : []));
  }, []);

  return { history, record, remove, clear };
}
