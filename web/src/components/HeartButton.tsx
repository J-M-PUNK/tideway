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
 * Run a brief one-shot animation when `active` flips from false to
 * true. Triggering on the transition (not on every render where
 * `active` is already true) is what makes this a satisfying micro-
 * interaction rather than a perpetually animating element. Hook
 * lives at the shared component level so every consumer threads the
 * same animation contract.
 *
 * Used by:
 *   - HeartButton + every place that toggles a favorite (heart-pop, 360 ms)
 *   - Download / Saved transitions (saved-pop, 280 ms)
 *   - Artist Follow's Heart→Check swap (heart-pop, 360 ms)
 */
export function useArrivalPulse(active: boolean, durationMs = 360): boolean {
  const [pulsing, setPulsing] = useState(false);
  const prevRef = useRef(active);
  useEffect(() => {
    const wasActive = prevRef.current;
    prevRef.current = active;
    // Only animate on the false→true transition. Reverting is a
    // dismissal action; pulsing the element as it disappears would
    // read as accidental enthusiasm.
    if (!wasActive && active) {
      setPulsing(true);
      const t = window.setTimeout(() => setPulsing(false), durationMs);
      return () => window.clearTimeout(t);
    }
  }, [active, durationMs]);
  return pulsing;
}

/**
 * Heart-pop variant — same useArrivalPulse hook with the 360 ms
 * heart-pop animation timing. Kept as a separate name so the
 * heart-toggle call sites read intentionally rather than passing a
 * magic number.
 */
function useHeartPop(active: boolean): boolean {
  return useArrivalPulse(active, 360);
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
