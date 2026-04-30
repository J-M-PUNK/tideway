import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Heart, Music, Video as VideoIcon } from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist, Track, Video } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { useColumnCount } from "@/hooks/useColumnCount";
import { useLikedByArtist } from "@/hooks/useLikedByArtist";
import { useSpotifyTrackPlaycountBatch } from "@/hooks/useSpotifyEnrichment";
import { useVideoPlayer } from "@/hooks/useVideoPlayer";
import { ArtistHero } from "@/components/ArtistHero";
import { ArtistTopCities } from "@/components/ArtistTopCities";
import { Grid, SectionHeader, ViewMoreLink } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import {
  GridSkeleton,
  HeroSkeleton,
  TrackListSkeleton,
} from "@/components/Skeletons";
import { VideoCard } from "@/components/VideoCard";
import { imageProxy } from "@/lib/utils";

export function ArtistDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  // Single round trip. The backend now parallelizes the eight Tidal
  // calls it needs and bundles credits + videos into the response,
  // so the frontend doesn't waterfall three separate fetches on
  // mount like it used to.
  const { data: artist, loading, error } = useApi(() => api.artist(id), [id]);
  const [popularExpanded, setPopularExpanded] = useState(false);
  // The user's liked tracks + albums credited to this artist. Spotify
  // renders a "You Liked" summary card above Albums; clicking it
  // opens the full list. Hook returns null while the first
  // library fetch is in flight; we just don't render the section in
  // that window.
  const liked = useLikedByArtist(artist?.id);
  // Batch-fetch Spotify playcounts for the top tracks in one
  // request, instead of letting each TrackList row fire its own.
  // Same fix as AlbumDetail; see hook for cost / benefit detail.
  useSpotifyTrackPlaycountBatch(artist?.top_tracks);

  if (loading) {
    return (
      <div>
        <HeroSkeleton />
        <SectionHeader title="Popular" />
        <TrackListSkeleton count={5} />
        <SectionHeader title="Discography" />
        <GridSkeleton count={6} />
      </div>
    );
  }
  if (error || !artist)
    return <ErrorView error={error ?? "Artist not found"} />;

  // "Download full discography" needs a single merged list of everything
  // the artist has released (albums + EPs + singles; skip appears-on
  // since those are someone else's records).
  const fullCatalog = [...artist.albums, ...artist.ep_singles];

  return (
    <div>
      <ArtistHero
        artistId={artist.id}
        artistName={artist.name}
        picture={artist.picture}
        topTracks={artist.top_tracks}
        allAlbums={fullCatalog}
        shareUrl={artist.share_url}
        onDownload={onDownload}
        artistMixId={artist.artist_mix_id}
      />

      {artist.top_tracks.length > 0 && (
        <>
          <SectionHeader title="Popular" />
          <TrackList
            tracks={
              popularExpanded
                ? artist.top_tracks
                : artist.top_tracks.slice(0, 5)
            }
            onDownload={onDownload}
            numbered
            showPlaycount
          />
          {artist.top_tracks.length > 5 && (
            <button
              onClick={() => setPopularExpanded((v) => !v)}
              className="mb-8 mt-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
            >
              {popularExpanded ? "Show less" : "View more"}
            </button>
          )}
        </>
      )}

      {liked && (liked.tracks.length > 0 || liked.albums.length > 0) && (
        <>
          <SectionHeader title="You Liked" />
          <LikedSummaryCard
            artistId={id}
            artistName={artist.name}
            tracks={liked.tracks}
            albums={liked.albums}
          />
        </>
      )}

      {/* Spotify-style "Popular" row — mixed-format top releases ranked
       *  by popularity with a recency boost (computed server-side).
       *  Sits above Albums / EPs because it surfaces the artist's most
       *  relevant work first regardless of release type. No "View more"
       *  — this is a curated cap, not a slice of a bigger list. */}
      <MediaRow
        title="Popular releases"
        items={artist.popular_releases}
        onDownload={onDownload}
      />
      <MediaRow
        title="Albums"
        items={artist.albums}
        viewMoreTo={`/artist/${id}/all/albums`}
        onDownload={onDownload}
      />
      <MediaRow
        title="EPs & Singles"
        items={artist.ep_singles}
        viewMoreTo={`/artist/${id}/all/eps`}
        onDownload={onDownload}
      />
      {/* Music videos sit above Appears on. The artist's own video
       *  output is more representative of "their work" than guest
       *  appearances on someone else's album, so it deserves the
       *  higher slot. Appears on is closer to a footnote. */}
      {artist.videos.length > 0 && (
        <VideoRow
          videos={artist.videos}
          viewMoreTo={`/artist/${id}/all/videos`}
        />
      )}
      <MediaRow
        title="Appears on"
        items={artist.appears_on}
        viewMoreTo={`/artist/${id}/all/appears-on`}
        onDownload={onDownload}
      />
      <MediaRow
        title="Fans also like"
        items={artist.similar}
        viewMoreTo={`/artist/${id}/all/similar`}
      />

      {artist.credits.length > 0 && (
        <>
          <SectionHeader title="Credits" />
          <TrackList
            tracks={artist.credits}
            onDownload={onDownload}
            showAlbum
          />
        </>
      )}

      <ArtistTopCities
        artistId={artist.id}
        artistName={artist.name}
        sampleIsrcs={artist.top_tracks
          .map((t) => t.isrc)
          .filter((s): s is string => !!s)
          .slice(0, 5)}
      />

      {artist.bio && (
        <>
          <SectionHeader title="About" />
          <ArtistBio bio={artist.bio} />
        </>
      )}
    </div>
  );
}

/**
 * Summary card for the "You Liked" section on the artist page.
 * Mirrors Spotify's surface — single cover image with a heart
 * overlay, primary text "X songs · Y release(s)", subtitle
 * "By [Artist]". The whole card is clickable; click opens the
 * dedicated drill-down at /artist/:id/all/liked.
 *
 * Cover preference order: first liked album cover (most stable
 * across re-fetches because albums change less often than the
 * track list), then first liked track's album cover, then a
 * placeholder Music glyph. The card never renders without at
 * least one liked item — caller gates on count > 0.
 */
function LikedSummaryCard({
  artistId,
  artistName,
  tracks,
  albums,
}: {
  artistId: string;
  artistName: string;
  tracks: Track[];
  albums: Album[];
}) {
  const songCount = tracks.length;
  const releaseCount = albums.length;
  // "Title" reads more naturally with the larger count first; the
  // user is more likely to have many liked songs and few liked
  // albums than the reverse, but either order works.
  const summary = [
    songCount > 0 ? `${songCount} ${songCount === 1 ? "song" : "songs"}` : null,
    releaseCount > 0
      ? `${releaseCount} ${releaseCount === 1 ? "release" : "releases"}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const cover = albums[0]?.cover ?? tracks[0]?.album?.cover ?? null;
  const coverUrl = cover ? imageProxy(cover) : null;

  return (
    <Link
      to={`/artist/${artistId}/all/liked`}
      className="mb-10 inline-flex max-w-md items-center gap-4 rounded-lg p-2 transition-colors hover:bg-accent"
    >
      <div className="relative h-20 w-20 shrink-0 overflow-hidden rounded-md bg-secondary">
        {coverUrl ? (
          <img
            src={coverUrl}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-8 w-8" />
          </div>
        )}
        <div className="absolute bottom-1 right-1 flex h-6 w-6 items-center justify-center rounded-full bg-primary text-primary-foreground shadow">
          <Heart className="h-3.5 w-3.5 fill-current" />
        </div>
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-base font-bold">{summary}</div>
        <div className="truncate text-sm text-muted-foreground">
          By {artistName}
        </div>
      </div>
    </Link>
  );
}

/**
 * One artist-page section (Albums, EPs/Singles, Appears on, similar
 * artists) — single row of cards at the current breakpoint with a
 * "View more" link that lands on the dedicated section page. Matches
 * the one-row + view-more convention used on Home, Album, Stats.
 */
function MediaRow<T extends Album | Artist>({
  title,
  items,
  viewMoreTo,
  onDownload,
}: {
  title: string;
  items: T[];
  /** Omit on curated rows that aren't a slice of a larger list (e.g.
   *  "Popular releases" — there is no "all popular releases" page to
   *  link to). When omitted, no link is rendered even if the row is
   *  capped by the breakpoint. */
  viewMoreTo?: string;
  onDownload?: OnDownload;
}) {
  const cols = useColumnCount();
  const visible = useMemo(() => items.slice(0, cols), [items, cols]);
  if (items.length === 0) return null;
  const hasMore = !!viewMoreTo && items.length > cols;

  return (
    <div>
      <div className="mb-4 mt-8 flex items-baseline justify-between gap-4">
        <h2 className="text-2xl font-bold tracking-tight">{title}</h2>
        {hasMore && <ViewMoreLink to={viewMoreTo!} />}
      </div>
      <Grid>
        {visible.map((item) => (
          <MediaCard key={item.id} item={item} onDownload={onDownload} />
        ))}
      </Grid>
    </div>
  );
}

/**
 * Videos row — single row capped at the current breakpoint with a
 * "View more" link. Cards are wider-than-tall thumbnails; clicking
 * opens the shared video modal since there's no per-video detail page.
 */
function VideoRow({
  videos,
  viewMoreTo,
}: {
  videos: Video[];
  viewMoreTo: string;
}) {
  const cols = useColumnCount();
  const { open } = useVideoPlayer();
  const visible = videos.slice(0, cols);
  const hasMore = videos.length > cols;
  return (
    <div>
      <div className="mb-4 mt-8 flex items-baseline justify-between gap-4">
        <h2 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <VideoIcon className="h-6 w-6" /> Videos
        </h2>
        {hasMore && <ViewMoreLink to={viewMoreTo} />}
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
        {visible.map((v) => (
          <VideoCard key={v.id} video={v} onPlay={() => open(v, videos)} />
        ))}
      </div>
    </div>
  );
}

function ArtistBio({ bio }: { bio: string }) {
  const [expanded, setExpanded] = useState(false);
  // Tidal bios sometimes include inline markers like `[wimpLink artistId="..."] ... [/wimpLink]`.
  // Strip them so the body reads cleanly. Memoized so we don't re-regex on
  // every render (bios can be 20KB+).
  const cleaned = useMemo(
    () => bio.replace(/\[wimpLink[^\]]*\]/g, "").replace(/\[\/wimpLink\]/g, ""),
    [bio],
  );
  const truncated =
    cleaned.length > 800 && !expanded
      ? cleaned.slice(0, 800).trimEnd() + "…"
      : cleaned;
  return (
    <div className="max-w-3xl rounded-lg border border-border/50 bg-card/40 p-6">
      <p className="whitespace-pre-line text-sm leading-relaxed text-muted-foreground">
        {truncated}
      </p>
      {cleaned.length > 800 && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mt-3 text-xs font-semibold uppercase tracking-wider text-primary hover:underline"
        >
          {expanded ? "Show less" : "Read more"}
        </button>
      )}
    </div>
  );
}
