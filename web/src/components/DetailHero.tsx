import { Music } from "lucide-react";
import { cn, imageProxy } from "@/lib/utils";
import { useCoverColor } from "@/hooks/useCoverColor";

interface Props {
  eyebrow: string;
  title: string;
  cover: string | null;
  meta?: React.ReactNode;
  round?: boolean;
  actions?: React.ReactNode;
  /**
   * When true, render the cover art as a large blurred backdrop behind
   * the hero (mimicking Tidal's album page). Falls back to the plain
   * dominant-color gradient when no cover is available. Default false
   * so Artist / Playlist / User pages keep the simpler treatment.
   */
  blurredBackdrop?: boolean;
}

/**
 * Shared hero for Album / Artist / Playlist / Mix detail pages — big cover
 * on the left, eyebrow / title / metadata on the right, optional action row
 * below. The background fades from the dominant color of the cover to the
 * page background, the way Spotify's detail pages do.
 */
export function DetailHero({
  eyebrow,
  title,
  cover,
  meta,
  round,
  actions,
  blurredBackdrop = false,
}: Props) {
  const src = imageProxy(cover);
  const dominant = useCoverColor(src);

  // Non-backdrop pages keep the old dominant-color gradient wash.
  const bgStyle: React.CSSProperties = dominant
    ? { background: `linear-gradient(180deg, ${dominant}, transparent 70%)` }
    : { background: "linear-gradient(180deg, hsl(0 0% 15%), transparent 70%)" };

  return (
    <div
      className="relative -mx-8 -mt-6 mb-6 overflow-hidden px-8 pb-6 pt-6 transition-colors duration-500"
      style={blurredBackdrop ? undefined : bgStyle}
    >
      {blurredBackdrop && src && (
        <>
          {/* Heavily blurred, enlarged cover behind everything. `scale`
           * hides the bleed that blur() creates at the edges; `aria-
           * hidden` keeps it out of the accessibility tree. */}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0"
            style={{
              backgroundImage: `url(${src})`,
              backgroundSize: "cover",
              backgroundPosition: "center",
              filter: "blur(48px) saturate(1.15)",
              transform: "scale(1.25)",
            }}
          />
          {/* Gradient fade: darken the whole thing, then fade into
           * transparent at the bottom so content below the hero flows
           * back into the page's normal background. */}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0"
            style={{
              background:
                "linear-gradient(180deg, rgba(0,0,0,0.35) 0%, rgba(0,0,0,0.55) 60%, hsl(var(--background)) 100%)",
            }}
          />
        </>
      )}
      <div className="relative flex flex-col items-end gap-6 md:flex-row">
        <div
          className={cn(
            "h-56 w-56 flex-shrink-0 overflow-hidden bg-secondary shadow-2xl",
            round ? "rounded-full" : "rounded-md",
          )}
        >
          {src ? (
            <img src={src} alt={title} className="h-full w-full object-cover" />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Music className="h-10 w-10" />
            </div>
          )}
        </div>
        <div className="min-w-0 flex-1 pb-4">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {eyebrow}
          </div>
          <h1
            className={cn(
              "mt-2 font-black tracking-tight",
              title.length > 30 ? "text-4xl" : "text-5xl",
            )}
          >
            {title}
          </h1>
          {meta && <div className="mt-4 text-sm text-muted-foreground">{meta}</div>}
        </div>
      </div>
      {actions && (
        <div className="relative mt-6 flex flex-wrap items-center gap-3">
          {actions}
        </div>
      )}
    </div>
  );
}
