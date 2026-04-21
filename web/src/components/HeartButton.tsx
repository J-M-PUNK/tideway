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
      <Heart className={cn(iconSize, liked && "fill-current")} />
    </Button>
  );
}
