import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { AddToLibraryButton } from "@/components/AddToLibraryButton";
import { AlbumCreditsButton } from "@/components/AlbumCreditsButton";
import { AlbumCreditsView } from "@/components/AlbumCreditsView";
import { CollectionOverflowMenu } from "@/components/CollectionOverflowMenu";
import { DetailHero } from "@/components/DetailHero";
import { ShareButton } from "@/components/ShareButton";
import { ShuffleButton } from "@/components/ShuffleButton";
import { PlayAllButton } from "@/components/PlayAllButton";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { SectionHeader } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { useLastfmAlbumPlaycount } from "@/hooks/useLastfmPlaycount";
import { useSpotifyAlbumTotalPlays } from "@/hooks/useSpotifyEnrichment";
import { cn, formatDuration, imageProxy } from "@/lib/utils";

export function AlbumDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const { data: album, loading, error } = useApi(() => api.album(id), [id]);
  // Tidal-style Credits "tab": toggling the Credits button swaps the
  // normal TrackList body for a 2-column grid of per-track credits.
  const [showingCredits, setShowingCredits] = useState(false);
  // Album-cover lightbox. Clicking the cover in the hero opens this;
  // no separate surface to open credits — that's what the Credits
  // button is for.
  const [coverOpen, setCoverOpen] = useState(false);

  if (loading) {
    return (
      <div>
        <HeroSkeleton />
        <div className="mt-10">
          <TrackListSkeleton />
        </div>
      </div>
    );
  }
  if (error || !album) return <ErrorView error={error ?? "Album not found"} />;

  const primaryArtist = album.artists[0];
  const artistForPill = primaryArtist
    ? {
        id: primaryArtist.id,
        name: primaryArtist.name,
        picture: primaryArtist.picture ?? null,
      }
    : undefined;

  return (
    <div>
      <DetailHero
        title={album.name}
        cover={album.cover}
        blurredBackdrop
        byArtist={artistForPill}
        onCoverClick={() => setCoverOpen(true)}
        coverHint="Expand cover"
        meta={
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              <span>
                {album.num_tracks} {album.num_tracks === 1 ? "track" : "tracks"}
                {album.duration ? ` (${formatDuration(album.duration)})` : ""}
              </span>
              <AlbumQualityBadge tags={album.media_tags ?? []} />
            </div>
            <div className="flex items-center gap-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {album.year && <span>{album.year}</span>}
              <AlbumPlaycountBadge
                artist={album.artists[0]?.name ?? ""}
                album={album.name}
                trackIsrcs={album.tracks
                  .map((t) => t.isrc)
                  .filter((i): i is string => !!i)}
              />
            </div>
          </div>
        }
        actions={
          <>
            <PlayAllButton
              tracks={album.tracks}
              source={{ type: "ALBUM", id: album.id }}
            />
            <ShuffleButton
              tracks={album.tracks}
              source={{ type: "ALBUM", id: album.id }}
            />
            <div className="ml-auto flex items-center gap-6">
              <AddToLibraryButton kind="album" id={album.id} />
              <AlbumCreditsButton
                showing={showingCredits}
                onToggle={() => setShowingCredits((v) => !v)}
              />
              <ShareButton shareUrl={album.share_url} />
              <CollectionOverflowMenu
                tracks={album.tracks}
                downloadKind="album"
                downloadId={album.id}
              />
            </div>
          </>
        }
      />
      <div className="mt-8">
        {showingCredits ? (
          <AlbumCreditsView albumId={album.id} />
        ) : (
          <TrackList
            tracks={album.tracks}
            onDownload={onDownload}
            showAlbum={false}
            showPlaycount
            source={{ type: "ALBUM", id: album.id }}
          />
        )}
      </div>

      {!showingCredits && (
        <AlbumInfoFooter
          releaseDate={album.release_date ?? null}
          numTracks={album.num_tracks}
          duration={album.duration}
          copyright={album.copyright ?? null}
        />
      )}

      {album.review && (
        <>
          <SectionHeader title="About this album" />
          <AlbumReview review={album.review} />
        </>
      )}

      {album.more_by_artist.length > 0 && (
        <SingleRowSection
          title={`More by ${album.artists[0]?.name ?? "this artist"}`}
          viewAllHref={
            album.artists[0] ? `/artist/${album.artists[0].id}` : undefined
          }
          items={album.more_by_artist}
          onDownload={onDownload}
        />
      )}

      {album.similar.length > 0 && (
        <SingleRowSection
          title="You might also like"
          items={album.similar}
          onDownload={onDownload}
        />
      )}

      {album.related_artists.length > 0 && (
        <SingleRowSection
          title="Fans also like"
          items={album.related_artists}
          onDownload={onDownload}
        />
      )}

      <CoverLightbox
        open={coverOpen}
        onOpenChange={setCoverOpen}
        cover={album.cover}
        title={album.name}
      />
    </div>
  );
}

/**
 * Full-size album-cover lightbox. Opened by clicking the cover in
 * the hero; closes on ESC / overlay click / the Dialog's built-in
 * close button. Matches how Tidal / Spotify / Apple Music surface the
 * "make the art bigger" affordance without committing to a separate
 * page. Uses the widest size the image proxy can serve.
 */
function CoverLightbox({
  open,
  onOpenChange,
  cover,
  title,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  cover: string | null;
  title: string;
}) {
  const src = imageProxy(cover);
  if (!src) return null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[min(90vw,90vh)] border-0 bg-transparent p-0 shadow-none">
        <img
          src={src}
          alt={title}
          className="block h-auto w-full rounded-md shadow-2xl"
        />
      </DialogContent>
    </Dialog>
  );
}

/**
 * Single horizontal row of cards capped to the visible column count,
 * with a "View more" link on the right. Mirrors the treatment used on
 * Home / Explore so album-page related sections feel consistent with
 * the rest of the app. Hides the view-more link when there's no
 * sensible destination to route to.
 */
function SingleRowSection({
  title,
  viewAllHref,
  items,
  onDownload,
}: {
  title: string;
  viewAllHref?: string;
  items: (Album | Artist)[];
  onDownload: OnDownload;
}) {
  return (
    <div className="mb-10">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h2 className="text-xl font-bold tracking-tight">{title}</h2>
        {viewAllHref && (
          <Link
            to={viewAllHref}
            className="flex flex-shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground"
          >
            View more <ChevronRight className="h-3 w-3" />
          </Link>
        )}
      </div>
      <div className="grid gap-4 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 2xl:grid-cols-6">
        {items.slice(0, 6).map((it, i) => (
          // Hide items past the current breakpoint's column count so
          // the section is always exactly one row — previously the
          // grid wrapped 6 items onto 2 rows at lg (5 cols) with 1
          // orphan item on row 2. Column counts: base=2, sm=3, md=4,
          // lg=5, 2xl=6, matched by the responsive hide/show classes
          // on each item.
          <div
            key={it.id}
            className={cn(
              ROW_ITEM_VISIBILITY[i],
              "min-w-0",
            )}
          >
            <MediaCard item={it} onDownload={onDownload} />
          </div>
        ))}
      </div>
    </div>
  );
}

// Per-index visibility so each item appears only at breakpoints
// where the grid has enough columns to fit it on the first row.
//   i=0,1: always visible (base has 2 cols)
//   i=2: sm and above (3 cols)
//   i=3: md and above   (4 cols)
//   i=4: lg and above   (5 cols)
//   i=5: 2xl and above  (6 cols)
const ROW_ITEM_VISIBILITY = [
  "",
  "",
  "hidden sm:block",
  "hidden md:block",
  "hidden lg:block",
  "hidden 2xl:block",
];

/**
 * Footer under the tracklist — release date, track count, runtime,
 * and the copyright line (which on most Tidal albums contains the
 * record label). Any individual field that's missing gets dropped
 * instead of showing "Unknown" junk.
 */
function AlbumInfoFooter({
  releaseDate,
  numTracks,
  duration,
  copyright,
}: {
  releaseDate: string | null;
  numTracks: number;
  duration: number;
  copyright: string | null;
}) {
  const formatted = releaseDate ? formatReleaseDate(releaseDate) : null;
  const runtime = duration ? formatDurationLong(duration) : null;
  return (
    <div className="mb-10 mt-10 text-sm text-muted-foreground">
      {formatted && <div>{formatted}</div>}
      {(numTracks > 0 || runtime) && (
        <div>
          {numTracks > 0 && (
            <>
              {numTracks} {numTracks === 1 ? "track" : "tracks"}
            </>
          )}
          {numTracks > 0 && runtime && ", "}
          {runtime}
        </div>
      )}
      {copyright && <div className="mt-2 text-xs">{copyright}</div>}
    </div>
  );
}

function formatReleaseDate(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

/**
 * Human runtime like "53 min 42 sec" or "1 hr 12 min". We already
 * have formatDuration for the clock-format "53:42" used inline in
 * the hero — the footer version is wordier to match Tidal's styling.
 */
function formatDurationLong(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = Math.floor(totalSeconds % 60);
  if (hours > 0) {
    return `${hours} hr ${minutes} min`;
  }
  if (minutes > 0) {
    return `${minutes} min ${seconds} sec`;
  }
  return `${seconds} sec`;
}

/**
 * Inline stats appended to the album meta row. Shows Spotify's
 * summed-across-tracks total play count (the headline "this album
 * has been played X billion times" number) and the user's personal
 * Last.fm scrobble count. The Last.fm-wide "listeners / plays"
 * fields are dropped — Last.fm's sample size is too small to make
 * those numbers useful next to Spotify's.
 */
function AlbumPlaycountBadge({
  artist,
  album,
  trackIsrcs,
}: {
  artist: string;
  album: string;
  trackIsrcs: string[];
}) {
  const pc = useLastfmAlbumPlaycount(artist, album);
  const spotify = useSpotifyAlbumTotalPlays(
    trackIsrcs.length > 0 ? trackIsrcs : null,
  );

  const user = pc?.userplaycount ?? 0;
  const totalPlays = spotify?.total_plays ?? 0;
  // When some tracks couldn't be resolved, append a "+" to signal
  // the number is a lower bound rather than the exact total.
  const partial =
    spotify != null && spotify.total > 0 && spotify.resolved < spotify.total;

  if (totalPlays <= 0 && user <= 0) return null;
  return (
    <>
      {totalPlays > 0 && (
        <span title={`Summed Spotify play count across ${spotify?.resolved ?? 0} of ${spotify?.total ?? 0} tracks`}>
          {formatCompact(totalPlays)}
          {partial ? "+" : ""} total plays
        </span>
      )}
      {user > 0 && (
        <span className="text-primary">
          you've played {user.toLocaleString()}{" "}
          {user === 1 ? "time" : "times"}
        </span>
      )}
    </>
  );
}

function formatCompact(n: number): string {
  if (n < 1000) return n.toLocaleString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

/**
 * Quality-tier pill next to the track-count line, matching Tidal's
 * own album-info badge. Shows the highest tier Tidal advertises for
 * the album:
 *   HI_RES_LOSSLESS        → "Max"      (primary color — FLAC 24/96+)
 *   LOSSLESS / HIRES       → "Lossless" (neutral — FLAC 16/44.1)
 *   DOLBY_ATMOS            → "Dolby Atmos"
 *   SONY_360RA             → "360 Reality Audio"
 *
 * Anything else (or an empty tags list) suppresses the badge — no
 * point announcing that a lossy album is lossy.
 */
function AlbumQualityBadge({ tags }: { tags: string[] }) {
  if (!tags || tags.length === 0) return null;
  const set = new Set(tags.map((t) => t.toUpperCase()));
  let label: string;
  let tone: string;
  let title: string;
  if (set.has("HIRES_LOSSLESS") || set.has("HI_RES_LOSSLESS")) {
    label = "Max";
    tone = "bg-primary/15 text-primary";
    title = "Hi-Res Lossless (FLAC, 24-bit / up to 192 kHz)";
  } else if (set.has("LOSSLESS") || set.has("HIRES")) {
    label = "Lossless";
    tone = "bg-foreground/10 text-foreground";
    title = "Lossless (FLAC 16-bit / 44.1 kHz)";
  } else if (set.has("DOLBY_ATMOS")) {
    label = "Dolby Atmos";
    tone = "bg-primary/15 text-primary";
    title = "Dolby Atmos immersive audio";
  } else if (set.has("SONY_360RA")) {
    label = "360 RA";
    tone = "bg-primary/15 text-primary";
    title = "Sony 360 Reality Audio";
  } else {
    return null;
  }
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider ${tone}`}
      title={title}
    >
      {label}
    </span>
  );
}

function AlbumReview({ review }: { review: string }) {
  // Strip Tidal's inline `[wimpLink]` anchors. Memoized so rerenders of the
  // parent don't re-regex the same string.
  const cleaned = useMemo(
    () => review.replace(/\[wimpLink[^\]]*\]/g, "").replace(/\[\/wimpLink\]/g, ""),
    [review],
  );
  return (
    <div className="max-w-3xl rounded-lg border border-border/50 bg-card/40 p-6">
      <p className="whitespace-pre-line text-sm leading-relaxed text-muted-foreground">
        {cleaned}
      </p>
    </div>
  );
}
