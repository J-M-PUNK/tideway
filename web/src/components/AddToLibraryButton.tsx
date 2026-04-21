import { Heart } from "lucide-react";
import type { FavoriteKind } from "@/api/types";
import { useFavorites } from "@/hooks/useFavorites";
import { cn } from "@/lib/utils";

/**
 * "Add to library" button for a detail-page actions row. Visually
 * matches `ShareButton`'s labeled pattern (icon above, small text
 * below) and uses the `useFavorites` hook under the hood — so
 * "Adding" an album/playlist/artist here is the same thing as hearting
 * it from a track row. Label flips to "Added" when the item is
 * already in the library, heart fills with the accent color.
 */
export function AddToLibraryButton({
  kind,
  id,
}: {
  kind: FavoriteKind;
  id: string;
}) {
  const favs = useFavorites();
  const liked = favs.has(kind, id);
  const label = liked ? "Added" : "Add";
  return (
    <button
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        favs.toggle(kind, id);
      }}
      className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground"
      title={liked ? "Remove from library" : "Add to library"}
      aria-pressed={liked}
    >
      <Heart
        className={cn("h-5 w-5", liked && "fill-primary text-primary")}
      />
      <div className="text-xs font-semibold">{label}</div>
    </button>
  );
}
