import { Link } from "react-router-dom";
import { Music, User as UserIcon } from "lucide-react";
import { cn, imageProxy } from "@/lib/utils";
import { useCoverColor } from "@/hooks/useCoverColor";

interface Props {
  eyebrow?: string;
  title: string;
  cover: string | null;
  meta?: React.ReactNode;
  round?: boolean;
  actions?: React.ReactNode;
  /**
   * When true, render the cover art as a large blurred backdrop behind
   * the hero (mimicking Tidal's album page). Also tightens vertical
   * spacing and drops the eyebrow text by default — Tidal-style hero
   * doesn't need an "ALBUM" label when the blurred cover already
   * signals context. Falls back to the plain dominant-color gradient
   * when no cover is available.
   */
  blurredBackdrop?: boolean;
  /**
   * Optional artist row rendered as a small avatar-pill under the
   * title. Used on the album page so the primary artist is clickable
   * without crowding the meta row with link markup.
   */
  byArtist?: { id: string; name: string; picture: string | null };
  /**
   * If provided, the cover becomes clickable and fires this handler.
   * Tidal uses this on the album page to toggle the credits view —
   * the caller decides what the click does.
   */
  onCoverClick?: () => void;
  /** Tooltip shown when hovering a clickable cover. */
  coverHint?: string;
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
  byArtist,
  onCoverClick,
  coverHint,
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
      <div
        className={cn(
          "relative flex flex-col items-end md:flex-row",
          blurredBackdrop ? "gap-5" : "gap-6",
        )}
      >
        {(() => {
          const cls = cn(
            "flex-shrink-0 overflow-hidden bg-secondary shadow-2xl",
            blurredBackdrop ? "h-60 w-60" : "h-56 w-56",
            round ? "rounded-full" : "rounded-md",
            onCoverClick &&
              "cursor-pointer transition-transform hover:scale-[1.02]",
          );
          const inner = src ? (
            <img src={src} alt={title} className="h-full w-full object-cover" />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Music className="h-10 w-10" />
            </div>
          );
          return onCoverClick ? (
            <button
              type="button"
              onClick={onCoverClick}
              title={coverHint}
              aria-label={coverHint}
              className={cls}
            >
              {inner}
            </button>
          ) : (
            <div className={cls}>{inner}</div>
          );
        })()}
        <div className="min-w-0 flex-1 pb-2">
          {eyebrow && (
            <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {eyebrow}
            </div>
          )}
          <h1
            className={cn(
              "font-black tracking-tight",
              eyebrow ? "mt-2" : "",
              blurredBackdrop
                ? title.length > 30
                  ? "text-3xl"
                  : "text-4xl"
                : title.length > 30
                  ? "text-4xl"
                  : "text-5xl",
            )}
          >
            {title}
          </h1>
          {byArtist && <ArtistPill artist={byArtist} />}
          {meta && (
            <div
              className={cn(
                "text-sm text-muted-foreground",
                byArtist ? "mt-3" : "mt-4",
              )}
            >
              {meta}
            </div>
          )}
        </div>
      </div>
      {actions && (
        <div
          className={cn(
            "relative flex flex-wrap items-center gap-3",
            blurredBackdrop ? "mt-5" : "mt-6",
          )}
        >
          {actions}
        </div>
      )}
    </div>
  );
}

function ArtistPill({
  artist,
}: {
  artist: { id: string; name: string; picture: string | null };
}) {
  const pic = imageProxy(artist.picture);
  return (
    <Link
      to={`/artist/${artist.id}`}
      className="mt-3 inline-flex items-center gap-2 text-sm font-semibold text-foreground hover:underline"
    >
      <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center overflow-hidden rounded-full bg-secondary">
        {pic ? (
          <img src={pic} alt="" className="h-full w-full object-cover" />
        ) : (
          <UserIcon className="h-3.5 w-3.5 text-muted-foreground" />
        )}
      </span>
      {artist.name}
    </Link>
  );
}
