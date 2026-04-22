import { Moon } from "lucide-react";
import { usePlayerActions, usePlayerSleep } from "@/hooks/PlayerContext";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

/**
 * Dropdown in the Now Playing bar for the sleep timer. Shows a pulse + the
 * remaining countdown when active.
 */
export function SleepTimerButton() {
  const { sleepRemaining } = usePlayerSleep();
  const { setSleepTimer, clearSleepTimer } = usePlayerActions();

  const active = sleepRemaining !== null;
  const label = formatRemaining(sleepRemaining);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn(
            "h-8 w-8 relative data-[state=open]:text-primary",
            active && "text-primary",
          )}
          title={active ? `Sleep timer: ${label}` : "Sleep timer"}
        >
          <Moon className="h-4 w-4" />
          {active && (
            <span className="absolute -bottom-0.5 right-0 text-[9px] font-bold tabular-nums">
              {label}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Sleep timer</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {active && (
          <>
            <DropdownMenuItem onSelect={clearSleepTimer}>
              <span className="text-destructive">Turn off</span>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
          </>
        )}
        {[15, 30, 45, 60].map((m) => (
          <DropdownMenuItem key={m} onSelect={() => setSleepTimer(m)}>
            {m} minutes
          </DropdownMenuItem>
        ))}
        <DropdownMenuItem onSelect={() => setSleepTimer("end-of-track")}>
          Stop at end of track
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function formatRemaining(ms: number | null): string {
  if (ms === null) return "";
  if (ms < 0) return "end";
  const totalSeconds = Math.ceil(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  if (m >= 1) return `${m}m`;
  return `${s}s`;
}
