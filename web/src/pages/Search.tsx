import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Loader2, Music, Search as SearchIcon } from "lucide-react";
import { api } from "@/api/client";
import type { SearchResponse, TopHit } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Grid, SectionHeader } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { TopHitCard } from "@/components/TopHitCard";
import { TrackList } from "@/components/TrackList";
import { EmptyState } from "@/components/EmptyState";
import {
  FormatFilter,
  type AudioFormat,
  hasAnyFormatTags,
  matchesFormat,
} from "@/components/FormatFilter";

type Filter = "all" | "tracks" | "albums" | "artists" | "playlists";

/**
 * Search page layout
 * ------------------
 * On the "All" tab we render a Spotify-style hero row at the top:
 * Top Result (Tidal-nominated best match) on the left, Songs column on
 * the right. Below the hero we keep the existing per-type rows in
 * order Artists → Albums → Playlists. The hero hides when the user
 * picks a specific tab — that view is meant to be a single dense list
 * of one kind, not a curated landing.
 */

export function Search({ onDownload }: { onDownload: OnDownload }) {
  // The query lives in the URL so the NavBar's search input and this
  // page stay in sync. Typing into either updates ?q=<value> and this
  // page re-fetches whenever that value changes.
  const [params] = useSearchParams();
  const q = params.get("q") ?? "";
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [format, setFormat] = useState<AudioFormat>("all");
  const debounceRef = useRef<number | null>(null);

  useEffect(() => {
    if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    const query = q.trim();
    if (!query) {
      setResults(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    // Guard against a slow response overwriting newer state — if the query
    // changed while this search was in-flight, discard its result.
    let cancelled = false;
    debounceRef.current = window.setTimeout(async () => {
      try {
        const res = await api.search(query, 16);
        if (!cancelled) setResults(res);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      if (debounceRef.current !== null)
        window.clearTimeout(debounceRef.current);
    };
  }, [q]);

  const filteredTracks =
    results && format !== "all"
      ? results.tracks.filter((t) => matchesFormat(t, format))
      : (results?.tracks ?? []);
  const filteredAlbums =
    results && format !== "all"
      ? results.albums.filter((a) => matchesFormat(a, format))
      : (results?.albums ?? []);

  // The top hit only respects the format filter when it's a kind that
  // actually has a media_tags field (track / album). Artists and
  // playlists pass through unconditionally.
  const topHit: TopHit | null = (() => {
    const t = results?.top_hit;
    if (!t) return null;
    if (format === "all") return t;
    if (t.kind === "track" || t.kind === "album") {
      return matchesFormat(t, format) ? t : null;
    }
    return t;
  })();

  const hasAny =
    !!results &&
    (filteredTracks.length > 0 ||
      filteredAlbums.length > 0 ||
      results.artists.length > 0 ||
      results.playlists.length > 0 ||
      topHit !== null);

  const showAll = filter === "all";
  const showTracks =
    results && (showAll || filter === "tracks") && filteredTracks.length > 0;
  const showAlbums =
    results && (showAll || filter === "albums") && filteredAlbums.length > 0;
  const showArtists =
    results && (showAll || filter === "artists") && results.artists.length > 0;
  const showPlaylists =
    results &&
    (showAll || filter === "playlists") &&
    results.playlists.length > 0;
  const showFormatFilter =
    !!results &&
    (showAll || filter === "tracks" || filter === "albums") &&
    hasAnyFormatTags([...results.tracks, ...results.albums]);

  // Hero row only appears on the "All" tab. The Songs-column on the
  // right is a compact slice of the track results so the user can
  // peek the top tracks without scrolling. Capped at 4 to keep the
  // hero balanced against the top-hit card height.
  //
  // When the top hit is itself a track, drop it from the Songs column
  // so the same row doesn't render twice — the hero already shows it.
  const topHitTrackId = topHit?.kind === "track" ? topHit.id : null;
  const tracksWithoutTopHit = topHitTrackId
    ? filteredTracks.filter((t) => t.id !== topHitTrackId)
    : filteredTracks;
  const heroTracks = tracksWithoutTopHit.slice(0, 4);
  const showHero = showAll && (topHit !== null || heroTracks.length > 0);

  // Don't double-render tracks: when the hero takes the first 4 (after
  // the top-hit dedupe), the Tracks row below starts from there. When
  // the hero is hidden, the row starts from 0 like before.
  const tracksBelowHero = showHero
    ? tracksWithoutTopHit.slice(heroTracks.length)
    : filteredTracks;

  return (
    <div>
      {loading && (
        <div className="mb-4 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Searching…
        </div>
      )}

      {results && hasAny && (
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <Tabs value={filter} onValueChange={(v) => setFilter(v as Filter)}>
            <TabsList>
              <TabsTrigger value="all">All</TabsTrigger>
              <TabsTrigger value="tracks">Tracks</TabsTrigger>
              <TabsTrigger value="albums">Albums</TabsTrigger>
              <TabsTrigger value="artists">Artists</TabsTrigger>
              <TabsTrigger value="playlists">Playlists</TabsTrigger>
            </TabsList>
          </Tabs>
          {showFormatFilter && (
            <FormatFilter value={format} onChange={setFormat} />
          )}
        </div>
      )}

      {!results && !q && (
        <EmptyState
          icon={SearchIcon}
          title="Search Tidal"
          description="Start typing in the search bar at the top to find tracks, albums, artists, or playlists."
        />
      )}

      {results && !hasAny && (
        <EmptyState
          icon={Music}
          title="No results"
          description={`Nothing matched "${q}".`}
        />
      )}

      {showHero && (
        <div className="mb-8 grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
          {topHit ? (
            <div>
              <SectionHeader title="Top result" />
              <TopHitCard hit={topHit} trackContext={filteredTracks} />
            </div>
          ) : (
            // Empty left column when Tidal didn't pick a top hit but we
            // still have tracks. Keeps the right column from stretching
            // full-width; on small screens this collapses naturally.
            <div className="hidden lg:block" />
          )}
          {heroTracks.length > 0 && (
            <div>
              <SectionHeader title="Songs" />
              <TrackList tracks={heroTracks} onDownload={onDownload} />
            </div>
          )}
        </div>
      )}

      {showTracks && tracksBelowHero.length > 0 && (
        <>
          <SectionHeader
            title={showHero && showAll ? "More tracks" : "Tracks"}
          />
          <TrackList
            tracks={tracksBelowHero.slice(0, filter === "tracks" ? 999 : 6)}
            onDownload={onDownload}
          />
        </>
      )}
      {showArtists && (
        <>
          <SectionHeader title="Artists" />
          <Grid>
            {results!.artists.map((a) => (
              <MediaCard key={a.id} item={a} />
            ))}
          </Grid>
        </>
      )}
      {showAlbums && (
        <>
          <SectionHeader title="Albums" />
          <Grid>
            {filteredAlbums.map((a) => (
              <MediaCard key={a.id} item={a} onDownload={onDownload} />
            ))}
          </Grid>
        </>
      )}
      {showPlaylists && (
        <>
          <SectionHeader title="Playlists" />
          <Grid>
            {results!.playlists.map((p) => (
              <MediaCard key={p.id} item={p} onDownload={onDownload} />
            ))}
          </Grid>
        </>
      )}
    </div>
  );
}
