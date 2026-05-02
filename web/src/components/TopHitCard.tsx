import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Music, Play } from "lucide-react";
import type { Track, TopHit } from "@/api/types";
import { PlayMediaButton } from "@/components/PlayMediaButton";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { cn, imageProxy } from "@/lib/utils";

/**
 * Hero card for the search page's "Top Result". Larger and busier than
 * a regular MediaCard: oversized cover, kind label as a chip, big
 * title, contextual subtitle, hover-revealed play button. Renders any
 * of the four searchable kinds (track / album / artist / playlist).
 *
 * Click target depends on kind: track plays inline (no detail page
 * exists for an individual track); the others navigate to their
 * detail page and offer playback through the hover button.
 */

const KIND_LABEL: Record<TopHit["kind"], string> = {
  track: "Song",
  album: "Album",
  artist: "Artist",
  playlist: "Playlist",
};

export function TopHitCard({
  hit,
  trackContext,
}: {
  hit: TopHit;
  /** Tracks the player should treat as the queue when the hero is a
   *  track. Typically the search-results track list so next/prev keep
   *  flowing through the same result set. Ignored for other kinds. */
  trackContext?: Track[];
}) {
  const navigate = useNavigate();

  const cover = imageProxy(
    hit.kind === "track"
      ? (hit.album?.cover ?? null)
      : hit.kind === "artist"
        ? hit.picture
        : hit.cover,
  );
  const isCircle = hit.kind === "artist";

  const detailHref =
    hit.kind === "artist"
      ? `/artist/${hit.id}`
      : hit.kind === "album"
        ? `/album/${hit.id}`
        : hit.kind === "playlist"
          ? `/playlist/${hit.id}`
          : null;

  const body = (
    <>
      <div
        className={cn(
          "relative h-40 w-40 overflow-hidden bg-secondary shadow-md",
          isCircle ? "rounded-full" : "rounded-md",
        )}
      >
        {cover ? (
          <img
            src={cover}
            alt={hit.name}
            className="h-full w-full object-cover transition-transform duration-300 ease-out group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-14 w-14" />
          </div>
        )}
      </div>
      <div className="min-w-0">
        <div
          className="truncate text-2xl font-bold lg:text-3xl"
          title={hit.name}
        >
          {hit.name}
        </div>
        {hit.kind !== "artist" && (
          <div className="mt-1 line-clamp-2 text-sm text-muted-foreground">
            <Subtitle hit={hit} onNavigate={navigate} />
          </div>
        )}
        <div className="mt-3">
          <span className="inline-flex rounded-full bg-secondary px-2.5 py-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {KIND_LABEL[hit.kind]}
          </span>
        </div>
      </div>
    </>
  );

  // No `h-full` — the search hero is a 2-column grid where the right
  // column (Songs list) is often much taller than the natural height
  // of this card. Stretching the card to match leaves a big empty
  // bottom region, especially on the artist branch where the subtitle
  // line is intentionally suppressed. Letting the card size to its
  // content matches the rhythm Spotify / Apple Music use, where the
  // top-result and songs columns can have different heights.
  const cardClasses =
    "group relative flex flex-col gap-5 rounded-lg bg-card p-6 transition-colors duration-200 ease-out hover:bg-accent";

  if (hit.kind === "track") {
    return (
      <TrackCardWrapper
        track={hit}
        context={trackContext}
        className={cardClasses}
      >
        {body}
        <TrackPlayCorner />
      </TrackCardWrapper>
    );
  }

  return (
    <Link to={detailHref!} className={cardClasses}>
      {body}
      {hit.kind !== "artist" && (
        <PlayCornerForMedia kind={hit.kind} id={hit.id} />
      )}
    </Link>
  );
}

/** Subtitle row. Tracks/albums show their artists as inline-clickable
 *  spans (so navigating to an artist from the hero doesn't follow the
 *  enclosing card's link/click). Artists show a generic descriptor.
 *  Playlists show the creator. */
function Subtitle({
  hit,
  onNavigate,
}: {
  hit: TopHit;
  onNavigate: (path: string) => void;
}) {
  // Artist hero skips the subtitle entirely — the kind chip is enough
  // and we don't have follower / popularity data on hand.
  if (hit.kind === "artist") return null;
  if (hit.kind === "track" || hit.kind === "album") {
    return (
      <>
        {hit.artists.map((a, i) => (
          <span key={a.id}>
            {i > 0 && ", "}
            <button
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onNavigate(`/artist/${a.id}`);
              }}
              className="hover:text-foreground hover:underline"
            >
              {a.name}
            </button>
          </span>
        ))}
        {hit.kind === "album" && hit.year && <span> · {hit.year}</span>}
      </>
    );
  }
  // Playlist
  const hasCreatorLink =
    hit.creator && hit.creator_id && hit.creator_id !== "0";
  return (
    <>
      {hit.creator ? (
        <>
          By{" "}
          {hasCreatorLink ? (
            <button
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onNavigate(`/user/${hit.creator_id}`);
              }}
              className="hover:text-foreground hover:underline"
            >
              {hit.creator}
            </button>
          ) : (
            <span>{hit.creator}</span>
          )}
        </>
      ) : (
        <>{hit.num_tracks} tracks</>
      )}
    </>
  );
}

/** Hover-revealed play button for album / playlist hero card.
 *  Reuses the standard PlayMediaButton (album/playlist fetch + play
 *  first track with full tracklist as context). */
function PlayCornerForMedia({
  kind,
  id,
}: {
  kind: "album" | "playlist";
  id: string;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const visibility = menuOpen
    ? "opacity-100"
    : "opacity-0 group-hover:opacity-100 focus-within:opacity-100";
  return (
    <div
      className={cn(
        "absolute bottom-5 right-5 transition-all duration-200 ease-out",
        visibility,
      )}
    >
      <PlayMediaButton
        kind={kind}
        id={id}
        className="h-14 w-14"
        onOpenChange={setMenuOpen}
      />
    </div>
  );
}

/** Wrapper that turns the whole card into a play-track click target
 *  when the hero is a track. Spread inline rather than via a Link
 *  because tracks don't have a detail page — clicking the card *is*
 *  the play action.
 *
 *  Rendered as a `div` with role=button (not a real `<button>`) so
 *  the artist-link buttons in the subtitle aren't nested inside a
 *  button (invalid HTML). Keyboard activation is restored via the
 *  Enter / Space handler. */
function TrackCardWrapper({
  track,
  context,
  className,
  children,
}: {
  track: Track;
  context?: Track[];
  className: string;
  children: React.ReactNode;
}) {
  const actions = usePlayerActions();
  const play = () => {
    const queue = context && context.length ? context : [track];
    actions.play(track, queue);
  };
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={play}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          play();
        }
      }}
      aria-label={`Play ${track.name}`}
      className={cn(className, "cursor-pointer text-left")}
    >
      {children}
    </div>
  );
}

/** Decorative hover affordance for the track hero. The whole card is
 *  already a play button — this is just the visual cue that hovering
 *  intends "click to play". A real <button> here would nest a button
 *  inside a button (invalid HTML), so it's a styled span. */
function TrackPlayCorner() {
  return (
    <span
      aria-hidden
      className={cn(
        "pointer-events-none absolute bottom-5 right-5 flex h-14 w-14 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg transition-all duration-200 ease-out",
        "opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100",
      )}
    >
      <Play className="h-5 w-5 fill-current" />
    </span>
  );
}
