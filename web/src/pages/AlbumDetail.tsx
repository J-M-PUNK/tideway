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
import { useLastfmAlbumPlaycount } from "@/hooks/useLastfmPlaycount";
import { formatDuration } from "@/lib/utils";

export function AlbumDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const { data: album, loading, error } = useApi(() => api.album(id), [id]);
  // Tidal-style Credits "tab": toggling the Credits button swaps the
  // normal TrackList body for a 2-column grid of per-track credits.
  const [showingCredits, setShowingCredits] = useState(false);

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
        onCoverClick={() => setShowingCredits((v) => !v)}
        coverHint={showingCredits ? "Back to tracklist" : "Show credits"}
        meta={
          <div className="flex flex-col gap-1.5">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {album.num_tracks} {album.num_tracks === 1 ? "track" : "tracks"}
              {album.duration ? ` (${formatDuration(album.duration)})` : ""}
            </div>
            <div className="flex items-center gap-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {album.year && <span>{album.year}</span>}
              <AlbumPlaycountBadge
                artist={album.artists[0]?.name ?? ""}
                album={album.name}
              />
            </div>
          </div>
        }
        actions={
          <>
            <PlayAllButton tracks={album.tracks} />
            <ShuffleButton tracks={album.tracks} />
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
          />
        )}
      </div>

      {album.review && (
        <>
          <SectionHeader title="About this album" />
          <AlbumReview review={album.review} />
        </>
      )}

      {!showingCredits && (
        <AlbumInfoFooter
          releaseDate={album.release_date ?? null}
          numTracks={album.num_tracks}
          duration={album.duration}
          copyright={album.copyright ?? null}
        />
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
    </div>
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
        {items.slice(0, 6).map((it) => (
          <MediaCard key={it.id} item={it} onDownload={onDownload} />
        ))}
      </div>
    </div>
  );
}

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
 * Inline Last.fm context appended to the album meta row. Renders up to
 * two parts:
 *   · 24K listeners · 120K plays      (global — always when available)
 *   · You've played 47 times          (personal — when scrobbled)
 * Suppresses itself entirely if both are empty/zero.
 */
function AlbumPlaycountBadge({ artist, album }: { artist: string; album: string }) {
  const pc = useLastfmAlbumPlaycount(artist, album);
  if (!pc) return null;
  const user = pc.userplaycount ?? 0;
  const listeners = pc.listeners ?? 0;
  const plays = pc.playcount ?? 0;
  if (user <= 0 && listeners <= 0 && plays <= 0) return null;
  return (
    <>
      {listeners > 0 && <span>{formatCompact(listeners)} listeners</span>}
      {plays > 0 && <span>{formatCompact(plays)} plays</span>}
      {user > 0 && (
        <span className="text-primary">you: {user.toLocaleString()}</span>
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
