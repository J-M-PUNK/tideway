import { useEffect, useState } from "react";
import { Speaker } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAudioOptions } from "@/hooks/useAudioOptions";
import { useToast } from "@/components/toast";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";

/**
 * Unified Sound output picker.
 *
 * One dropdown — same icon — covers both local audio devices
 * (CoreAudio / WASAPI / ALSA outputs) and remote Cast devices on
 * the LAN. Tidal's desktop client lays it out the same way: local
 * devices on top, then a "Chromecast" section below for the cast
 * targets. Single mental model, one click to switch destination.
 *
 * Per-device "More settings" follows the Tidal pattern too — the
 * currently-selected local device gets a small (More settings)
 * link beside its label that opens a dialog with Exclusive Mode
 * and Force Volume. Those toggles don't apply to Cast targets, so
 * they only show up when a local device is the active one.
 *
 * Value namespace in the radio group is prefixed (`local:`,
 * `cast:`) so we can route a selection to either the audio engine
 * or the Cast manager without ambiguity. Local IDs include the
 * empty-string "system default," which becomes `"local:"` —
 * that's fine; the prefix-and-split logic handles it.
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
  };
  devices: CastDeviceSummary[];
};

const LOCAL_PREFIX = "local:";
const CAST_PREFIX = "cast:";

export function OutputDevicePicker() {
  const toast = useToast();
  const opts = useAudioOptions();
  const [cast, setCast] = useState<CastDevicesResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);

  // Light Cast polling so the picker reflects mDNS arrivals /
  // departures and detects backend-initiated disconnects (Cast
  // device powers off mid-session, etc.). Same cadence the
  // standalone CastPicker used; cheap (one HTTP round-trip on a
  // 5s interval, off the hot path).
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const res = await api.cast.devices();
        if (!cancelled) setCast(res);
      } catch {
        // Cast isn't a guaranteed feature; failures are silent
        // and the section just doesn't render.
      }
    };
    void refresh();
    const handle = window.setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, []);

  const refreshAll = async () => {
    await Promise.all([
      opts.refresh(),
      api.cast
        .devices()
        .then((res) => setCast(res))
        .catch(() => {}),
    ]);
  };

  // What the radio shows as selected. Casting wins — if a session
  // is open, the Cast device is the active sound output. Otherwise
  // we surface the local device id.
  const castConnectedId = cast?.status.connected_id ?? null;
  const selectedValue = castConnectedId
    ? `${CAST_PREFIX}${castConnectedId}`
    : `${LOCAL_PREFIX}${opts.current}`;

  const showCastSection =
    cast !== null &&
    cast.status.available &&
    (cast.devices.length > 0 || castConnectedId !== null);

  const onSelect = async (value: string) => {
    if (busy || value === selectedValue) return;
    setBusy(true);
    try {
      if (value.startsWith(CAST_PREFIX)) {
        const deviceId = value.slice(CAST_PREFIX.length);
        const result = await api.cast.connect(deviceId);
        toast.show({
          kind: "success",
          title: `Casting to ${result.device.friendly_name}`,
          description: "Audio is streaming to the device.",
        });
      } else if (value.startsWith(LOCAL_PREFIX)) {
        const localId = value.slice(LOCAL_PREFIX.length);
        // If we're currently casting, switching to a local
        // device implies stopping the cast. Order matters:
        // disconnect cast first so the encoder closes cleanly,
        // THEN flip the local output. Otherwise the brief
        // overlap can produce a stutter on the local device as
        // the audio engine rebinds while still feeding the cast
        // ring buffer.
        if (castConnectedId !== null) {
          await api.cast.disconnect();
        }
        await opts.setDevice(localId);
      }
      await refreshAll();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.show({
        kind: "error",
        title: "Couldn't switch output",
        description: message,
      });
      // Refresh anyway — backend may have changed state in
      // spite of the error response.
      await refreshAll();
    } finally {
      setBusy(false);
    }
  };

  const flipExclusive = async (v: boolean) => {
    try {
      await opts.setExclusiveMode(v);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't update Exclusive Mode",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const flipForceVolume = async (v: boolean) => {
    try {
      await opts.setForceVolume(v);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't update Force Volume",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  // Trigger appearance: highlights when a Cast session is active
  // OR when Exclusive Mode is on, because both are "audio's not
  // going to default speakers" states the user might want at-a-
  // glance confirmation of.
  const triggerHighlight = castConnectedId !== null || opts.exclusiveMode;
  const currentLocalName =
    opts.devices.find((d) => d.id === opts.current)?.name ?? "System default";
  const triggerTitle = castConnectedId
    ? `Casting to ${cast?.status.connected_name ?? "device"}`
    : `Output: ${currentLocalName}`;

  return (
    <>
      <DropdownMenu
        onOpenChange={(open) => {
          if (open) void refreshAll();
        }}
      >
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className={cn(
              "h-8 w-8 data-[state=open]:text-primary",
              triggerHighlight && "text-primary",
            )}
            title={triggerTitle}
          >
            <Speaker className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-96">
          <DropdownMenuLabel>Sound output</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuRadioGroup
            value={selectedValue}
            onValueChange={onSelect}
          >
            {opts.loaded && opts.devices.length > 0 ? (
              opts.devices.map((d) => {
                const value = `${LOCAL_PREFIX}${d.id}`;
                const isSelected =
                  value === selectedValue && castConnectedId === null;
                return (
                  <DropdownMenuRadioItem
                    key={d.id || "default"}
                    value={value}
                    onSelect={(e) => e.preventDefault()}
                    className="text-sm"
                    disabled={busy}
                  >
                    <span className="flex-1">{d.name}</span>
                    {isSelected && (
                      <button
                        type="button"
                        onClick={(e) => {
                          // Stop the radio item from also firing
                          // its select handler — the user is asking
                          // for the per-device dialog, not a re-
                          // select of the already-active device.
                          e.preventDefault();
                          e.stopPropagation();
                          setMoreOpen(true);
                        }}
                        className="ml-2 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                      >
                        (More settings)
                      </button>
                    )}
                  </DropdownMenuRadioItem>
                );
              })
            ) : (
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                {opts.loaded ? "Audio engine not available." : "Loading…"}
              </div>
            )}

            {showCastSection && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuLabel className="text-muted-foreground">
                  Chromecast
                </DropdownMenuLabel>
                {cast.devices.length === 0 ? (
                  <div className="px-2 py-1.5 text-xs text-muted-foreground">
                    No Cast devices currently visible. Discovery is running;
                    powered-on devices appear within a few seconds.
                  </div>
                ) : (
                  cast.devices.map((d) => (
                    <DropdownMenuRadioItem
                      key={d.id}
                      value={`${CAST_PREFIX}${d.id}`}
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
                  ))
                )}
              </>
            )}
          </DropdownMenuRadioGroup>
          {castConnectedId !== null && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onSelect={() => {
                  void onSelect(`${LOCAL_PREFIX}${opts.current}`);
                }}
                className="text-xs text-muted-foreground"
              >
                Stop casting and return to local output
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <MoreSettingsDialog
        open={moreOpen}
        onOpenChange={setMoreOpen}
        deviceName={currentLocalName}
        exclusiveMode={opts.exclusiveMode}
        forceVolume={opts.forceVolume}
        onExclusiveChange={flipExclusive}
        onForceVolumeChange={flipForceVolume}
      />
    </>
  );
}

/**
 * Per-device options dialog. Reached via the (More settings) link
 * on the active local device. Tidal puts these toggles here
 * because they're per-device behaviors — Exclusive Mode means
 * "lock this device to Tideway," Force Volume means "control this
 * device's volume from Tideway at max." Cast targets have neither
 * concept (the receiver owns its own volume / sharing model), so
 * the dialog only opens for local outputs.
 */
function MoreSettingsDialog({
  open,
  onOpenChange,
  deviceName,
  exclusiveMode,
  forceVolume,
  onExclusiveChange,
  onForceVolumeChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  deviceName: string;
  exclusiveMode: boolean;
  forceVolume: boolean;
  onExclusiveChange: (v: boolean) => void;
  onForceVolumeChange: (v: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{deviceName}</DialogTitle>
          <DialogDescription>
            Per-device playback options. Apply only when this output is active.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4 pt-2">
          <ToggleRow
            checked={exclusiveMode}
            onChange={onExclusiveChange}
            label="Use Exclusive Mode"
            hint={
              <>
                Tideway takes exclusive use of the audio device. Default
                playback is already bit-perfect when the source rate matches the
                device, so leave this off unless you also want to lock other
                apps out of the device.
              </>
            }
          />
          <ToggleRow
            checked={forceVolume}
            onChange={onForceVolumeChange}
            label="Force volume"
            hint={
              <>
                Keep Tideway volume at max and control output on your external
                device.
              </>
            }
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ToggleRow({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="flex items-center gap-3 text-sm font-medium">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="h-4 w-4 accent-primary"
        />
        {label}
      </label>
      {hint && <p className="ml-7 text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}
