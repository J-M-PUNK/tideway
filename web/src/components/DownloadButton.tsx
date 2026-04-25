import { ChevronDown, Download } from "lucide-react";
import type { ContentKind } from "@/api/types";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useQualities } from "@/hooks/useQualities";
import { DOWNLOAD_GATE_TOOLTIP, useSubscription } from "@/hooks/useSubscription";
import { effectiveFormatLabel } from "@/lib/quality";
import { cn } from "@/lib/utils";

interface Props {
  kind: Extract<ContentKind, "track" | "album" | "playlist">;
  id: string;
  onPick: (
    kind: Extract<ContentKind, "track" | "album" | "playlist">,
    id: string,
    quality?: string,
  ) => void;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
  label?: string;
  iconOnly?: boolean;
  className?: string;
  onOpenChange?: (open: boolean) => void;
  /** Optional — when supplied, the Max tier shows a small badge
   *  saying whether the user will actually get hi-res FLAC for THIS
   *  track or a duplicate of the Lossless stream. */
  mediaTags?: string[];
}

export function DownloadButton({
  kind,
  id,
  onPick,
  variant,
  size,
  label,
  iconOnly,
  className,
  onOpenChange,
  mediaTags,
}: Props) {
  const qualities = useQualities() ?? [];
  const sub = useSubscription();

  const stop = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  if (!sub.canDownload) {
    return (
      <Button
        variant={variant}
        size={iconOnly ? "icon" : size}
        className={cn(className, "cursor-not-allowed opacity-50")}
        disabled
        title={sub.reason ?? DOWNLOAD_GATE_TOOLTIP}
      >
        <Download className="h-4 w-4" />
        {!iconOnly && <>{label ?? "Download"}</>}
      </Button>
    );
  }

  return (
    <DropdownMenu onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild onClick={stop}>
        <Button
          variant={variant}
          size={iconOnly ? "icon" : size}
          className={cn(className)}
          title="Download…"
        >
          <Download className={iconOnly ? "h-4 w-4" : "h-4 w-4"} />
          {!iconOnly && (
            <>
              {label ?? "Download"}
              <ChevronDown className="h-3.5 w-3.5 opacity-70" />
            </>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        onClick={stop}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DropdownMenuLabel>Download quality</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {qualities.map((q) => {
          const effective = effectiveFormatLabel(q.value, mediaTags);
          return (
            <DropdownMenuItem
              key={q.value}
              onSelect={() => onPick(kind, id, q.value)}
            >
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{q.label}</span>
                  <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    {q.codec}
                  </span>
                  {effective && (
                    <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
                      {effective}
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">{q.bitrate}</div>
                <div className="text-[11px] text-muted-foreground/70">{q.description}</div>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
