import { useEffect, useRef, useState } from "react";
import { Heart } from "lucide-react";
import type { FavoriteKind } from "@/api/types";
import { useFavorites } from "@/hooks/useFavorites";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Props {
  kind: FavoriteKind;
  id: string;
  size?: "sm" | "md";
  className?: string;
  /**
   * Whether the button is visible by default or only on hover.
   * Track rows prefer hover; detail pages keep it always visible.
   */
  visibility?: "always" | "hover";
  /**
   * Color of the unliked heart outline. Most surfaces want "muted"
   * so the icon recedes next to primary content; the now-playing
   * bar uses "foreground" so it stands out against the dark bar.
   * Has no effect on the liked state (always primary/accent).
   */
  tone?: "muted" | "foreground";
}

/**
 * Run a brief scale-pop animation when `liked` flips from false to
 * true. Triggering on the transition (not on every render where
 * `liked` is already true) is what makes this a satisfying micro-
 * interaction rather than a perpetually animating button. Hook
 * lives at the shared component level so every consumer of
 * HeartButton — track rows, now-playing bar, detail-page action
 * rows, the inline overlay on cards (see MediaCard's mirrored
 * implementation) — gets the same treatment for free.
 */
function useHeartPop(liked: boolean): boolean {
  const [popping, setPopping] = useState(false);
  const prevLikedRef = useRef(liked);
  useEffect(() => {
    const wasLiked = prevLikedRef.current;
    prevLikedRef.current = liked;
    // Only animate on the false→true transition. Unliking is a
    // dismissal action; popping the icon as it disappears would
    // read as accidental enthusiasm.
    if (!wasLiked && liked) {
      setPopping(true);
      const t = window.setTimeout(() => setPopping(false), 360);
      return () => window.clearTimeout(t);
    }
  }, [liked]);
  return popping;
}

export function HeartButton({
  kind,
  id,
  size = "md",
  className,
  visibility = "always",
  tone = "muted",
}: Props) {
  const favs = useFavorites();
  const liked = favs.has(kind, id);
  const popping = useHeartPop(liked);

  const label = liked ? `Unlike ${kind}` : `Like ${kind}`;
  const iconSize = size === "sm" ? "h-4 w-4" : "h-5 w-5";
  const btnSize = size === "sm" ? "h-7 w-7" : "h-9 w-9";

  const unlikedClass =
    tone === "foreground"
      ? "text-foreground hover:text-foreground"
      : "text-muted-foreground hover:text-foreground";

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        favs.toggle(kind, id);
      }}
      className={cn(
        btnSize,
        // Color transition smooths the un-liked → liked color flip
        // even on rapid double-toggles where the pop animation is
        // suppressed by reduced-motion.
        "transition-colors",
        liked ? "text-primary hover:text-primary" : unlikedClass,
        visibility === "hover" &&
          !liked &&
          "opacity-0 transition-opacity group-hover:opacity-100",
        className,
      )}
      title={label}
      aria-label={label}
      aria-pressed={liked}
    >
      <Heart
        className={cn(
          iconSize,
          liked && "fill-current",
          popping && "animate-heart-pop",
        )}
      />
    </Button>
  );
}

// Re-export the hook so MediaCard's InlineHeart (which builds its
// own button rather than going through this component) can apply
// the same animation contract without duplicating the timing logic.
export { useHeartPop };
