import { WifiOff } from "lucide-react";
import { useOfflineMode } from "@/hooks/useOfflineMode";

/**
 * Thin banner that surfaces across the top of the app when the
 * browser says the machine has lost network. Distinct from the
 * user's "Work offline" preference — if the user flipped that
 * themselves, no banner is shown (they already know). This is
 * only for the case where WiFi dropped / cable got unplugged /
 * the laptop woke from sleep with no connection, so the user
 * knows why Search, Explore, streaming, etc. suddenly don't work.
 *
 * Vanishes automatically when `navigator.onLine` flips back to
 * true — the user doesn't need to acknowledge.
 */
export function OfflineBanner() {
  const { offlineSource } = useOfflineMode();
  if (offlineSource !== "auto") return null;
  return (
    <div className="flex items-center justify-center gap-2 bg-amber-500/20 px-4 py-1.5 text-xs font-semibold text-amber-200">
      <WifiOff className="h-3.5 w-3.5" />
      You&apos;re offline. Switched to local library; streaming will resume when
      the connection returns.
    </div>
  );
}
