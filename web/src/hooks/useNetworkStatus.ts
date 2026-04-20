import { useEffect, useState } from "react";

/**
 * Browser-level online/offline signal, derived from `navigator.onLine`
 * plus the `online`/`offline` events the UA fires when the OS reports
 * a connectivity change.
 *
 * Intentionally simple. `navigator.onLine` only catches LAN-level
 * disconnects — it won't flag "WiFi connected but no route to the
 * internet" or a Tidal-specific outage. For those you'd need a periodic
 * health-check ping, which we don't bother with yet. The return value
 * here is good enough to drive the obvious case ("WiFi went down") and
 * lets us auto-flip the app into offline mode without persisting the
 * user's preference.
 */
export function useNetworkStatus(): boolean {
  // SSR-safe default. On first render in the browser this reads the
  // actual value; outside a browser it falls back to "online" so no
  // code path mistakes build-time rendering for a dead network.
  const [online, setOnline] = useState<boolean>(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  );

  useEffect(() => {
    const onOnline = () => setOnline(true);
    const onOffline = () => setOnline(false);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    // Resync on mount in case the status changed between module
    // evaluation and the effect running.
    setOnline(navigator.onLine);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  return online;
}
