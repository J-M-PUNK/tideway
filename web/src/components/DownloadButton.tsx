import { useEffect, useState } from "react";
import { Check, ChevronDown, Download } from "lucide-react";
import { api } from "@/api/client";
import type { ContentKind, Settings } from "@/api/types";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export interface QualityOption {
  value: string;
  label: string;
  codec: string;
  bitrate: string;
  description: string;
}

let _cachedQualities: QualityOption[] | null = null;
let _cachedSettings: Settings | null = null;

async function loadCatalog() {
  if (!_cachedQualities) _cachedQualities = await api.qualities();
  if (!_cachedSettings) _cachedSettings = await api.settings.get();
  return { qualities: _cachedQualities, settings: _cachedSettings };
}

interface Props {
  kind: Extract<ContentKind, "track" | "album" | "playlist">;
  id: string;
  onPick: (kind: Extract<ContentKind, "track" | "album" | "playlist">, id: string, quality?: string) => void;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
  label?: string;
  iconOnly?: boolean;
  className?: string;
  onOpenChange?: (open: boolean) => void;
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
}: Props) {
  const [qualities, setQualities] = useState<QualityOption[]>(_cachedQualities ?? []);
  const [defaultQuality, setDefaultQuality] = useState<string | null>(
    _cachedSettings?.quality ?? null,
  );

  useEffect(() => {
    if (_cachedQualities && _cachedSettings) return;
    loadCatalog().then(({ qualities, settings }) => {
      setQualities(qualities);
      setDefaultQuality(settings.quality);
    });
  }, []);

  const stop = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

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
          const isDefault = defaultQuality === q.value;
          return (
            <DropdownMenuItem
              key={q.value}
              onSelect={() => onPick(kind, id, q.value)}
            >
              <div className="mt-0.5 w-4 flex-shrink-0">
                {isDefault && <Check className="h-3.5 w-3.5 text-primary" />}
              </div>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{q.label}</span>
                  <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    {q.codec}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground">{q.bitrate}</div>
                <div className="text-[11px] text-muted-foreground/70">{q.description}</div>
              </div>
            </DropdownMenuItem>
          );
        })}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onPick(kind, id)}>
          <div className="w-4 flex-shrink-0" />
          <div className="flex flex-col">
            <span className="text-sm font-semibold">Use default</span>
            <span className="text-xs text-muted-foreground">
              {defaultQuality
                ? qualities.find((q) => q.value === defaultQuality)?.label ?? defaultQuality
                : "From Settings"}
            </span>
          </div>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
