import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "@/api/client";
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
import { Grid, SectionHeader } from "@/components/Grid";
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

  const artists = album.artists.map((a, i) => (
    <span key={a.id}>
      {i > 0 && <span className="text-muted-foreground"> · </span>}
      <Link to={`/artist/${a.id}`} className="font-semibold text-foreground hover:underline">
        {a.name}
      </Link>
    </span>
  ));

  return (
    <div>
      <DetailHero
        eyebrow="Album"
        title={album.name}
        cover={album.cover}
        meta={
          <div className="flex flex-wrap items-center gap-x-2">
            {artists}
            {album.year && <span>· {album.year}</span>}
            <span>
              · {album.num_tracks} tracks · {formatDuration(album.duration)}
            </span>
            <AlbumPlaycountBadge
              artist={album.artists[0]?.name ?? ""}
              album={album.name}
            />
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

      {album.similar.length > 0 && (
        <>
          <SectionHeader title="Similar albums" />
          <Grid>
            {album.similar.map((a) => (
              <MediaCard key={a.id} item={a} onDownload={onDownload} />
            ))}
          </Grid>
        </>
      )}
    </div>
  );
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
      {listeners > 0 && <span>· {formatCompact(listeners)} listeners</span>}
      {plays > 0 && <span>· {formatCompact(plays)} plays</span>}
      {user > 0 && (
        <span className="text-primary">
          · you: {user.toLocaleString()}
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
