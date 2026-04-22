import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import type { DownloadItem } from "@/api/types";

/**
 * Fires a single OS-level notification when a burst of downloads
 * finishes. "Burst" = the active count drops from >0 to 0; we don't
 * notify per-track because a 20-track album would spam the user with
 * 20 notifications, which nobody asked for.
 *
 * Goes through `/api/notify` (osascript on macOS, PowerShell toast on
 * Windows, notify-send on Linux) rather than the browser's Notification
 * API. pywebview's embedded WKWebView doesn't always surface browser
 * notifications at the OS level, and the shell-out path is both more
 * reliable and doesn't require a permission prompt.
 *
 * Silently no-op when `enabled` is false. Runs unconditionally when
 * the pref is on, window focus doesn't matter here — a "downloads
 * done" notification is useful even if the window happens to be
 * focused, because the user may not be watching the downloads page.
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

    const wasActive = prevActive.current;
    const nowActive = active.length;
    const newCompleted = completed.length - prevCompleted.current;
    prevActive.current = nowActive;
    prevCompleted.current = completed.length;

    // Only fire when the queue just emptied AND at least one new item
    // actually completed. Cancelling the last active item drops active
    // to 0 without any completions — don't notify in that case.
    if (wasActive === 0 || nowActive !== 0 || newCompleted <= 0) return;

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
    api.notify(title, body);
  }, [enabled, active.length, completed.length]);
}
