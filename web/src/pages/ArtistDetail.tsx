import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { DetailHero } from "@/components/DetailHero";
import { Grid, SectionHeader } from "@/components/Grid";
import { HeartButton } from "@/components/HeartButton";
import { MediaCard } from "@/components/MediaCard";
import { PlayAllButton } from "@/components/PlayAllButton";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton, HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";

export function ArtistDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const { data: artist, loading, error } = useApi(() => api.artist(id), [id]);

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

  return (
    <div>
      <DetailHero
        eyebrow="Artist"
        title={artist.name}
        cover={artist.picture}
        round
        actions={
          <>
            {artist.top_tracks.length > 0 && (
              <PlayAllButton tracks={artist.top_tracks} />
            )}
            <HeartButton kind="artist" id={artist.id} />
          </>
        }
      />

      {artist.top_tracks.length > 0 && (
        <>
          <SectionHeader title="Popular" />
          <TrackList tracks={artist.top_tracks} onDownload={onDownload} numbered />
        </>
      )}

      {artist.albums.length > 0 && (
        <>
          <SectionHeader title="Discography" />
          <Grid>
            {artist.albums.map((a) => (
              <MediaCard key={a.id} item={a} onDownload={onDownload} />
            ))}
          </Grid>
        </>
      )}

      {artist.similar.length > 0 && (
        <>
          <SectionHeader title="Fans also like" />
          <Grid>
            {artist.similar.map((a) => (
              <MediaCard key={a.id} item={a} />
            ))}
          </Grid>
        </>
      )}

      {artist.bio && (
        <>
          <SectionHeader title="About" />
          <ArtistBio bio={artist.bio} />
        </>
      )}
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
