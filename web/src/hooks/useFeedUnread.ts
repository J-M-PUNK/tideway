import { useEffect, useState } from "react";
import { api } from "@/api/client";

/**
 * Tracks how many items in the Feed are newer than the last time the
 * user visited the page. Backs the sidebar's Feed badge.
 *
 * Mechanics:
 *   - Module-level cache so every mount (Sidebar + FeedPage) shares one
 *     fetch instead of each triggering its own.
 *   - Polls every 5 minutes once the app is running — new releases
 *     trickle in rarely enough that this is cheap.
 *   - `lastSeen` is a UNIX ms timestamp persisted in localStorage;
 *     markFeedSeen() bumps it to "now" and re-notifies subscribers so
 *     the badge clears immediately.
 *   - First-launch behavior: if `lastSeen` is unset, we initialize it
 *     to the current time so a brand-new install doesn't show every
 *     historical release as "unread". Only items released AFTER the
 *     first launch will ever show up in the badge count.
 */
const LAST_SEEN_KEY = "tidal-downloader:feed-last-seen";
const POLL_INTERVAL_MS = 5 * 60 * 1000;

type FeedReleaseDate = { released_at: string };

let cachedItems: FeedReleaseDate[] | null = null;
let inFlight: Promise<void> | null = null;
const subs = new Set<() => void>();
let pollTimer: number | null = null;

function notify() {
  for (const fn of subs) fn();
}

function getLastSeen(): number {
  const raw = localStorage.getItem(LAST_SEEN_KEY);
  if (raw) {
    const parsed = parseInt(raw, 10);
    if (!isNaN(parsed)) return parsed;
  }
  // First launch — seed with now so we don't flood the badge on
  // initial install. Persist so subsequent reloads agree.
  const now = Date.now();
  localStorage.setItem(LAST_SEEN_KEY, String(now));
  return now;
}

async function refresh(): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const data = await api.feed();
      cachedItems = data.items || [];
      notify();
    } catch {
      /* Silent — next poll will retry. */
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

function ensurePolling() {
  if (pollTimer !== null) return;
  refresh();
  pollTimer = window.setInterval(refresh, POLL_INTERVAL_MS);
}

function stopPollingIfIdle() {
  if (subs.size > 0) return;
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/**
 * Mark the feed as read up to "now". Call from FeedPage on mount so
 * landing on the feed clears the badge.
 */
export function markFeedSeen() {
  localStorage.setItem(LAST_SEEN_KEY, String(Date.now()));
  notify();
}

/**
 * Count of feed items released after the user's last visit. Zero when
 * unknown — the badge simply doesn't render until the first fetch
 * completes.
 */
export function useFeedUnreadCount(): number {
  const [, tick] = useState(0);

  useEffect(() => {
    const sub = () => tick((n) => n + 1);
    subs.add(sub);
    ensurePolling();
    return () => {
      subs.delete(sub);
      stopPollingIfIdle();
    };
  }, []);

  if (!cachedItems) return 0;
  const lastSeen = getLastSeen();
  let count = 0;
  for (const item of cachedItems) {
    const ts = Date.parse(item.released_at);
    if (!isNaN(ts) && ts > lastSeen) count++;
  }
  return count;
}
