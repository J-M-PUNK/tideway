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
}

/**
 * Shared hero for Album / Artist / Playlist / Mix detail pages — big cover
 * on the left, eyebrow / title / metadata on the right, optional action row
 * below. The background fades from the dominant color of the cover to the
 * page background, the way Spotify's detail pages do.
 */
export function DetailHero({ eyebrow, title, cover, meta, round, actions }: Props) {
  const src = imageProxy(cover);
  const dominant = useCoverColor(src);

  // Negative margins pull the gradient out to the edges of the main area,
  // then we add equivalent padding so inner content aligns with the page.
  const bgStyle: React.CSSProperties = dominant
    ? {
        background: `linear-gradient(180deg, ${dominant}, transparent 70%)`,
      }
    : {
        background: "linear-gradient(180deg, hsl(0 0% 15%), transparent 70%)",
      };

  return (
    <div
      className="relative -mx-8 -mt-6 mb-6 px-8 pb-6 pt-6 transition-colors duration-500"
      style={bgStyle}
    >
      <div className="flex flex-col items-end gap-6 md:flex-row">
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
      {actions && <div className="mt-6 flex flex-wrap items-center gap-3">{actions}</div>}
    </div>
  );
}
