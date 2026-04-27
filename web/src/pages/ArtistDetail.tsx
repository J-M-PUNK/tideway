import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { Video as VideoIcon } from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist, Video } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { useColumnCount } from "@/hooks/useColumnCount";
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

export function ArtistDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  // Single round trip. The backend now parallelizes the eight Tidal
  // calls it needs and bundles credits + videos into the response,
  // so the frontend doesn't waterfall three separate fetches on
  // mount like it used to.
  const { data: artist, loading, error } = useApi(() => api.artist(id), [id]);
  const [popularExpanded, setPopularExpanded] = useState(false);

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
      <MediaRow
        title="Appears on"
        items={artist.appears_on}
        viewMoreTo={`/artist/${id}/all/appears-on`}
        onDownload={onDownload}
      />
      {artist.videos.length > 0 && (
        <VideoRow
          videos={artist.videos}
          viewMoreTo={`/artist/${id}/all/videos`}
        />
      )}
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
  viewMoreTo: string;
  onDownload?: OnDownload;
}) {
  const cols = useColumnCount();
  const visible = useMemo(() => items.slice(0, cols), [items, cols]);
  if (items.length === 0) return null;
  const hasMore = items.length > cols;

  return (
    <div>
      <div className="mb-4 mt-8 flex items-baseline justify-between gap-4">
        <h2 className="text-2xl font-bold tracking-tight">{title}</h2>
        {hasMore && <ViewMoreLink to={viewMoreTo} />}
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
