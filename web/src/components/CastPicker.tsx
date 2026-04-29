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

/**
 * Chromecast device picker. Shows up next to the volume / output-
 * device controls in the now-playing bar. Refreshes the device list
 * each time the dropdown opens; the backend keeps a continuous
 * mDNS browser running, so opening the picker after powering on a
 * speaker shows it within a couple of seconds.
 *
 * Phase 1 (this commit): discovery only. Selecting a device shows
 * a coming-soon toast — the actual session / sink wiring lands in
 * the next commit. Building the UI now keeps the verification loop
 * tight: we know discovery works on the user's network before we
 * sink time into the routing layer.
 *
 * The icon hides itself entirely when pychromecast isn't available
 * (broken wheel install) or when discovery has had no events for a
 * long time AND no devices are known. That keeps the cluster lean
 * for users on networks where Cast simply isn't reachable.
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
  };
  devices: CastDeviceSummary[];
};

const LOCAL_VALUE = "__local__";

export function CastPicker() {
  const toast = useToast();
  const [data, setData] = useState<CastDevicesResponse | null>(null);
  const [selected, setSelected] = useState<string>(LOCAL_VALUE);
  const [loading, setLoading] = useState(false);

  // Initial fetch so we know whether to render the icon at all.
  // Subsequent opens refetch via onOpenChange below; this is the
  // boot-time peek that decides whether the user even has Cast
  // devices on their LAN.
  useEffect(() => {
    let cancelled = false;
    void api.cast
      .devices()
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch(() => {
        // Silent — Cast isn't a guaranteed feature. The picker just
        // doesn't render; the rest of the app keeps working.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Hide the picker entirely when:
  //   - pychromecast didn't import (status.available = false)
  //   - discovery never started (status.running = false AND no
  //     devices ever surfaced)
  // We DO show it when discovery is running but no devices are
  // currently known — empty-state messaging in the dropdown is
  // useful diagnostic.
  if (data === null) return null;
  if (!data.status.available) return null;
  if (!data.status.running && data.devices.length === 0) return null;

  const refresh = async () => {
    setLoading(true);
    try {
      const res = await api.cast.devices();
      setData(res);
    } catch {
      // ignore — keep last good data
    } finally {
      setLoading(false);
    }
  };

  const onSelect = (next: string) => {
    if (next === LOCAL_VALUE) {
      setSelected(LOCAL_VALUE);
      return;
    }
    // Phase 1 stub. The session wiring that actually routes audio
    // lands in the next commit; for now, surface a toast so users
    // testing the discovery layer aren't confused by a click that
    // appears to do nothing.
    toast.show({
      kind: "info",
      title: "Cast routing coming soon",
      description:
        "Discovery works — your devices show up here. Audio routing to " +
        "Cast targets is still in progress and will land in a follow-up " +
        "release.",
    });
  };

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
          className="h-8 w-8 data-[state=open]:text-primary"
          title={
            data.devices.length > 0
              ? `Cast (${data.devices.length} device${data.devices.length === 1 ? "" : "s"})`
              : "Cast"
          }
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
          >
            <div className="flex flex-col">
              <span>This device</span>
              <span className="text-[11px] text-muted-foreground">
                Play through Tideway's local audio output.
              </span>
            </div>
          </DropdownMenuRadioItem>
          {data.devices.length === 0 && !loading && (
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
