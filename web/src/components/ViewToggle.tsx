import { LayoutGrid, Menu } from "lucide-react";
import { cn } from "@/lib/utils";

export type ViewMode = "grid" | "list";

/**
 * Two-segment toggle between tile/grid and list rendering. Shared
 * between the Library page (liked albums / artists / playlists) and
 * the Artist view-all page so the affordance and styling stay
 * consistent across surfaces that walk through a list of items.
 *
 * Stays a controlled component — caller owns the `ViewMode` state
 * and any persistence (localStorage etc.). The toggle just emits
 * the selected mode on click.
 */
export function ViewToggle({
  view,
  onChange,
}: {
  view: ViewMode;
  onChange: (v: ViewMode) => void;
}) {
  return (
    <div className="inline-flex rounded-md border border-border bg-secondary p-0.5">
      <button
        type="button"
        onClick={() => onChange("grid")}
        title="Grid view"
        aria-label="Grid view"
        aria-pressed={view === "grid"}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-sm transition-colors",
          view === "grid"
            ? "bg-background text-foreground"
            : "text-muted-foreground hover:text-foreground",
        )}
      >
        <LayoutGrid className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={() => onChange("list")}
        title="List view"
        aria-label="List view"
        aria-pressed={view === "list"}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-sm transition-colors",
          view === "list"
            ? "bg-background text-foreground"
            : "text-muted-foreground hover:text-foreground",
        )}
      >
        <Menu className="h-4 w-4" />
      </button>
    </div>
  );
}
