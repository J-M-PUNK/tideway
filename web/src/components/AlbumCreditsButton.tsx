import { FileText } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Labeled "Credits" action for the album detail hero. Matches the Add /
 * Share / More styling (icon + text below). Toggles a sibling
 * credits view on the Album page — parent owns the `showing` state so
 * the hero stays put when the user flips the view.
 */
export function AlbumCreditsButton({
  showing,
  onToggle,
}: {
  showing: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      className={cn(
        "flex flex-col items-center gap-1 transition-colors",
        showing
          ? "text-primary"
          : "text-muted-foreground hover:text-foreground",
      )}
      title="Credits"
      aria-label="View album credits"
      aria-pressed={showing}
    >
      <FileText className="h-5 w-5" />
      <div className="text-xs font-semibold">Credits</div>
    </button>
  );
}
