import { useEffect, useRef } from "react";
import type { DownloadItem } from "@/api/types";

/**
 * Fires a single desktop notification when a burst of downloads
 * finishes. "Burst" = the active count drops from >0 to 0; we don't
 * notify per-track because a 20-track album would spam the user with
 * 20 notifications, which nobody asked for.
 *
 * Permission is requested lazily — only the first time the user
 * enables the preference AND there's an in-flight download. That
 * sidesteps the "brand-new user loads the app and gets an instant
 * permission prompt" experience.
 *
 * Notifications silently no-op when:
 * - `enabled` is false
 * - the browser doesn't support Notification (older Safari, file://)
 * - the user has denied permission — we don't re-prompt
 */
export function useDownloadNotifications(
  enabled: boolean,
  active: DownloadItem[],
  completed: DownloadItem[],
) {
  // Track previous active count so we can detect the >0 → 0 transition.
  // Also track completed count so the notification body can say how
  // many new ones finished, not the cumulative total.
  const prevActive = useRef(active.length);
  const prevCompleted = useRef(completed.length);
  // Track last titles so a one-track completion can name the track.
  const lastCompletedTitles = useRef<string[]>([]);

  useEffect(() => {
    lastCompletedTitles.current = completed.map((c) => c.title);
  }, [completed]);

  useEffect(() => {
    if (!enabled) {
      prevActive.current = active.length;
      prevCompleted.current = completed.length;
      return;
    }
    if (typeof window === "undefined" || !("Notification" in window)) {
      return;
    }

    const wasActive = prevActive.current;
    const nowActive = active.length;
    const newCompleted = completed.length - prevCompleted.current;
    prevActive.current = nowActive;
    prevCompleted.current = completed.length;

    // Only fire when the queue just emptied AND at least one new item
    // actually completed. Cancelling the last active item drops active
    // to 0 without any completions — don't notify in that case.
    if (wasActive === 0 || nowActive !== 0 || newCompleted <= 0) return;

    const fire = () => {
      if (Notification.permission !== "granted") return;
      const titles = lastCompletedTitles.current.slice(-newCompleted);
      const title =
        newCompleted === 1
          ? "Download complete"
          : `${newCompleted} downloads complete`;
      const body =
        newCompleted === 1 && titles[0]
          ? titles[0]
          : titles.slice(0, 3).join(", ") +
            (newCompleted > 3 ? `, +${newCompleted - 3} more` : "");
      try {
        // Tag ensures a second burst replaces the first notification
        // instead of stacking a history, matching what Spotify / other
        // streaming apps do.
        new Notification(title, { body, tag: "tidal-downloader" });
      } catch {
        /* browsers may throw on permission-revoked; swallow */
      }
    };

    if (Notification.permission === "granted") {
      fire();
      return;
    }
    if (Notification.permission === "default") {
      // Ask lazily. If the user denies, we won't re-ask for this page
      // load (browsers also rate-limit repeat prompts).
      Notification.requestPermission()
        .then((perm) => {
          if (perm === "granted") fire();
        })
        .catch(() => {
          /* ignore */
        });
    }
    // 'denied' → silently skip.
  }, [enabled, active.length, completed.length]);
}
