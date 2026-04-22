import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { ChevronRight, Play, Video as VideoIcon } from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist, Track, Video } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { useColumnCount } from "@/hooks/useColumnCount";
import { useVideoPlayer } from "@/hooks/useVideoPlayer";
import { prefetchVideoStream } from "@/hooks/useVideoStream";
import { ArtistHero } from "@/components/ArtistHero";
import { ArtistTopCities } from "@/components/ArtistTopCities";
import { Grid, SectionHeader } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton, HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { formatDuration, imageProxy } from "@/lib/utils";

export function ArtistDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const { data: artist, loading, error } = useApi(() => api.artist(id), [id]);
  // Credits is a separate lazy fetch — Tidal's endpoint is undocumented
  // and may return nothing for some artists, so we load it in parallel
  // and hide the section when empty rather than blocking the main view.
  const [credits, setCredits] = useState<(Track & { role: string })[] | null>(null);
  const [videos, setVideos] = useState<Video[] | null>(null);
  // Default to top-5; reveal the full top-10 on click. Matches
  // Spotify / Apple Music's "Show more" behavior on artist pages
  // where the first five tracks are the headline and the rest are
  // discoverable with one extra interaction.
  const [popularExpanded, setPopularExpanded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setCredits(null);
    setVideos(null);
    api
      .artistCredits(id)
      .then((rows) => !cancelled && setCredits(rows))
      .catch(() => !cancelled && setCredits([]));
    api
      .artistVideos(id)
      .then((rows) => !cancelled && setVideos(rows))
      .catch(() => !cancelled && setVideos([]));
    return () => {
      cancelled = true;
    };
  }, [id]);

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
  if (error || !artist) return <ErrorView error={error ?? "Artist not found"} />;

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
              {popularExpanded ? "Show less" : "Show more"}
            </button>
          )}
        </>
      )}

      <MediaRow
        title="Albums"
        items={artist.albums}
        onDownload={onDownload}
      />
      <MediaRow
        title="EPs & Singles"
        items={artist.ep_singles}
        onDownload={onDownload}
      />
      <MediaRow
        title="Appears on"
        items={artist.appears_on}
        onDownload={onDownload}
      />
      {videos && videos.length > 0 && <VideoRow videos={videos} />}
      <MediaRow title="Fans also like" items={artist.similar} />

      {credits && credits.length > 0 && (
        <>
          <SectionHeader title="Credits" />
          <TrackList tracks={credits} onDownload={onDownload} showAlbum />
        </>
      )}

      <ArtistTopCities
        artistId={artist.id}
        sampleIsrc={artist.top_tracks.find((t) => t.isrc)?.isrc ?? null}
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
 * artists) — capped to a single row of cards at the current breakpoint
 * with a "Show all" toggle. The toggle reveals the rest inline;
 * clicking it again collapses back to a single row. Matches the
 * one-row-with-view-more pattern used on Home.
 */
function MediaRow<T extends Album | Artist>({
  title,
  items,
  onDownload,
}: {
  title: string;
  items: T[];
  onDownload?: OnDownload;
}) {
  const cols = useColumnCount();
  const [expanded, setExpanded] = useState(false);
  const visible = useMemo(
    () => (expanded ? items : items.slice(0, cols)),
    [items, cols, expanded],
  );
  if (items.length === 0) return null;
  const hasMore = items.length > cols;

  return (
    <div>
      <div className="mb-4 mt-8 flex items-baseline justify-between gap-4">
        <h2 className="text-2xl font-bold tracking-tight">{title}</h2>
        {hasMore && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex flex-shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
          >
            {expanded ? "Show less" : "Show all"}
            <ChevronRight
              className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-90" : ""}`}
            />
          </button>
        )}
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
 * Videos row — one row capped at the current breakpoint with a
 * Show all toggle, matching the Albums/EPs layout. Cards are
 * video-shaped (wider-than-tall thumbnails) and clicking opens the
 * shared video modal instead of navigating, since there's no per-
 * video detail page.
 */
function VideoRow({ videos }: { videos: Video[] }) {
  const cols = useColumnCount();
  const [expanded, setExpanded] = useState(false);
  const { open } = useVideoPlayer();
  const visible = expanded ? videos : videos.slice(0, cols);
  const hasMore = videos.length > cols;
  return (
    <div>
      <div className="mb-4 mt-8 flex items-baseline justify-between gap-4">
        <h2 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <VideoIcon className="h-6 w-6" /> Videos
        </h2>
        {hasMore && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex flex-shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
          >
            {expanded ? "Show less" : "Show all"}
            <ChevronRight
              className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-90" : ""}`}
            />
          </button>
        )}
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
        {visible.map((v) => (
          <VideoCard
            key={v.id}
            video={v}
            onPlay={() => open(v, videos)}
          />
        ))}
      </div>
    </div>
  );
}

function VideoCard({ video, onPlay }: { video: Video; onPlay: () => void }) {
  const cover = video.cover ? imageProxy(video.cover) : undefined;
  return (
    <button
      onClick={onPlay}
      onMouseEnter={() => prefetchVideoStream(video.id)}
      onFocus={() => prefetchVideoStream(video.id)}
      className="group flex flex-col gap-2 rounded-lg p-2 text-left transition-colors hover:bg-accent"
    >
      <div className="relative aspect-video overflow-hidden rounded-md bg-secondary">
        {cover ? (
          <img
            src={cover}
            alt=""
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <VideoIcon className="h-8 w-8" />
          </div>
        )}
        <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
          <Play className="h-8 w-8 text-foreground" fill="currentColor" />
        </span>
        {video.duration > 0 && (
          <span className="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold text-foreground">
            {formatDuration(video.duration)}
          </span>
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold">{video.name}</div>
        {video.artist && (
          <div className="truncate text-xs text-muted-foreground">{video.artist.name}</div>
        )}
      </div>
    </button>
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
  const truncated = cleaned.length > 800 && !expanded ? cleaned.slice(0, 800).trimEnd() + "…" : cleaned;
  return (
    <div className="max-w-3xl rounded-lg border border-border/50 bg-card/40 p-6">
      <p className="whitespace-pre-line text-sm leading-relaxed text-muted-foreground">{truncated}</p>
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
