import { useEffect, useState } from "react";
import { Cast } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useToast } from "@/components/toast";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";

/**
 * Chromecast device picker. Shows up next to the volume / output-
 * device controls in the now-playing bar. Refreshes the device list
 * each time the dropdown opens; the backend keeps a continuous
 * mDNS browser running, so opening the picker after powering on a
 * speaker shows it within a couple of seconds.
 *
 * Selecting a Cast device kicks off /api/cast/connect — the audio
 * engine begins encoding to FLAC and serving an HTTP stream that
 * the device fetches via play_media. Selecting "This device" calls
 * /api/cast/disconnect and audio returns to the local output.
 *
 * The icon hides itself entirely when pychromecast isn't available
 * (broken wheel install) or when discovery has had no events for a
 * long time AND no devices are known. That keeps the cluster lean
 * for users on networks where Cast simply isn't reachable. Once a
 * device IS connected we always render the icon with a
 * primary-colored highlight so the user can see at a glance that
 * audio is going elsewhere.
 */

type CastDeviceSummary = {
  id: string;
  friendly_name: string;
  model_name: string;
  manufacturer: string;
  cast_type: string;
};

type CastDevicesResponse = {
  status: {
    available: boolean;
    running: boolean;
    device_count: number;
    last_event_age_s: number | null;
    connected_id?: string | null;
    connected_name?: string | null;
    bytes_encoded?: number;
    media_loaded?: boolean;
  };
  devices: CastDeviceSummary[];
};

const LOCAL_VALUE = "__local__";

export function CastPicker() {
  const toast = useToast();
  const [data, setData] = useState<CastDevicesResponse | null>(null);
  const [busy, setBusy] = useState(false);

  // Initial fetch + light polling so the icon's connected-state
  // highlight reflects what the backend actually thinks. 5s is fine
  // — connecting is a manual action that the picker triggers, not
  // something we discover passively. The poll mostly handles the
  // case where the backend disconnects on its own (device went
  // offline) so the icon clears.
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const res = await api.cast.devices();
        if (!cancelled) setData(res);
      } catch {
        // silent — Cast isn't a guaranteed feature
      }
    };
    void refresh();
    const handle = window.setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, []);

  if (data === null) return null;
  if (!data.status.available) return null;
  if (
    !data.status.running &&
    data.devices.length === 0 &&
    !data.status.connected_id
  ) {
    return null;
  }

  const connectedId = data.status.connected_id ?? null;
  const connected = connectedId !== null;
  const selected = connectedId ?? LOCAL_VALUE;

  const refresh = async () => {
    try {
      const res = await api.cast.devices();
      setData(res);
    } catch {
      // ignore — keep last good data
    }
  };

  const onSelect = async (next: string) => {
    if (busy) return;
    if (next === selected) return;
    setBusy(true);
    try {
      if (next === LOCAL_VALUE) {
        await api.cast.disconnect();
        toast.show({
          kind: "success",
          title: "Stopped casting",
          description: "Audio is back on this device.",
        });
      } else {
        const device = data.devices.find((d) => d.id === next);
        const result = await api.cast.connect(next);
        toast.show({
          kind: "success",
          title: `Casting to ${result.device.friendly_name}`,
          description:
            device?.cast_type === "group"
              ? "Audio is streaming to the speaker group."
              : "Audio is streaming to the device.",
        });
      }
      await refresh();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.show({
        kind: "error",
        title: next === LOCAL_VALUE ? "Couldn't stop casting" : "Couldn't cast",
        description: message,
      });
      // Refresh anyway — backend may have changed state in spite
      // of the error response, and stale UI is worse than a
      // contradictory toast.
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const triggerTitle = connected
    ? `Casting to ${data.status.connected_name ?? "device"}`
    : data.devices.length > 0
      ? `Cast (${data.devices.length} device${data.devices.length === 1 ? "" : "s"})`
      : "Cast";

  return (
    <DropdownMenu
      onOpenChange={(open) => {
        if (open) void refresh();
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn(
            "h-8 w-8 data-[state=open]:text-primary",
            connected && "text-primary",
          )}
          title={triggerTitle}
        >
          <Cast className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuLabel>Cast to a device</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup value={selected} onValueChange={onSelect}>
          <DropdownMenuRadioItem
            value={LOCAL_VALUE}
            onSelect={(e) => e.preventDefault()}
            className="text-sm"
            disabled={busy}
          >
            <div className="flex flex-col">
              <span>This device</span>
              <span className="text-[11px] text-muted-foreground">
                Play through Tideway's local audio output.
              </span>
            </div>
          </DropdownMenuRadioItem>
          {data.devices.length === 0 && (
            <div className="px-2 py-1.5 text-xs text-muted-foreground">
              No Cast devices on your network.
              {data.status.last_event_age_s !== null
                ? ` Last seen activity ${Math.round(data.status.last_event_age_s)}s ago.`
                : " Discovery is running; powered-on devices will appear within a few seconds."}
            </div>
          )}
          {data.devices.map((d) => (
            <DropdownMenuRadioItem
              key={d.id}
              value={d.id}
              onSelect={(e) => e.preventDefault()}
              className="text-sm"
              disabled={busy}
            >
              <div className="flex flex-col">
                <span>{d.friendly_name}</span>
                <span className="text-[11px] text-muted-foreground">
                  {d.model_name || d.manufacturer || d.cast_type}
                </span>
              </div>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
