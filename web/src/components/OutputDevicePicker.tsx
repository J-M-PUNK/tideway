import { Speaker } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAudioOptions } from "@/hooks/useAudioOptions";
import { useToast } from "@/components/toast";
import { cn } from "@/lib/utils";

/**
 * Bottom-bar device picker with the audiophile toggles inline, same
 * place Tidal puts them. Picker mounts in both the active now-playing
 * bar and the empty-state bar; state lives in `useAudioOptions` so
 * a track-start/end doesn't refetch the device list and settings.
 */
export function OutputDevicePicker() {
  const toast = useToast();
  const opts = useAudioOptions();

  const withToast =
    <T,>(title: string, fn: (arg: T) => Promise<unknown>) =>
    async (arg: T) => {
      try {
        await fn(arg);
      } catch (err) {
        toast.show({
          kind: "error",
          title,
          description: err instanceof Error ? err.message : String(err),
        });
      }
    };

  const pickDevice = withToast("Couldn't switch output device", opts.setDevice);
  const flipExclusive = withToast(
    "Couldn't update Exclusive Mode",
    opts.setExclusiveMode,
  );
  const flipForceVolume = withToast(
    "Couldn't update Force Volume",
    opts.setForceVolume,
  );

  const currentName =
    opts.devices.find((d) => d.id === opts.current)?.name ?? "System default";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn(
            "h-8 w-8 data-[state=open]:text-primary",
            opts.exclusiveMode && "text-primary",
          )}
          title={`Output: ${currentName}`}
        >
          <Speaker className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuLabel>Output device</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {opts.loaded && opts.devices.length > 0 ? (
          <DropdownMenuRadioGroup value={opts.current} onValueChange={pickDevice}>
            {opts.devices.map((d) => (
              <DropdownMenuRadioItem
                key={d.id || "default"}
                value={d.id}
                onSelect={(e) => e.preventDefault()}
                className="text-sm"
              >
                {d.name}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        ) : (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            {opts.loaded ? "Audio engine not available." : "Loading…"}
          </div>
        )}

        <DropdownMenuSeparator />
        <DropdownMenuLabel>Options</DropdownMenuLabel>
        <DropdownMenuCheckboxItem
          checked={opts.exclusiveMode}
          onCheckedChange={(v) => flipExclusive(!!v)}
          onSelect={(e) => e.preventDefault()}
          className="items-start"
        >
          <div className="flex flex-col">
            <span className="font-semibold">Use Exclusive Mode</span>
            <span className="text-[11px] text-muted-foreground">
              Tideway takes exclusive use of the audio device. Default
              playback is already bit-perfect when the source rate
              matches the device, so leave this off unless you also
              want to lock other apps out of the device.
            </span>
          </div>
        </DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem
          checked={opts.forceVolume}
          onCheckedChange={(v) => flipForceVolume(!!v)}
          onSelect={(e) => e.preventDefault()}
          className="items-start"
        >
          <div className="flex flex-col">
            <span className="font-semibold">Force volume</span>
            <span className="text-[11px] text-muted-foreground">
              Keep Tideway volume at max and control output on your
              external device.
            </span>
          </div>
        </DropdownMenuCheckboxItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
