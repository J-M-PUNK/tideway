import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Loader2, Music, Search as SearchIcon } from "lucide-react";
import { api } from "@/api/client";
import type {
  Album,
  Artist,
  Playlist,
  SearchResponse,
  TopHit,
} from "@/api/types";
import type { OnDownload } from "@/api/download";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Grid, SectionHeader, ViewMoreLink } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { TopHitCard } from "@/components/TopHitCard";
import { TrackList } from "@/components/TrackList";
import { EmptyState } from "@/components/EmptyState";
import { useColumnCount } from "@/hooks/useColumnCount";
import {
  FormatFilter,
  type AudioFormat,
  hasAnyFormatTags,
  matchesFormat,
} from "@/components/FormatFilter";

type Filter = "all" | "tracks" | "albums" | "artists" | "playlists";
const FILTERS: Filter[] = ["all", "tracks", "albums", "artists", "playlists"];

function asFilter(v: string | null): Filter {
  return FILTERS.includes(v as Filter) ? (v as Filter) : "all";
}

/** Build a /search URL preserving the current query and pinning the
 *  given tab. "all" omits the tab param so the canonical landing URL
 *  stays clean. */
function tabUrl(q: string, tab: Filter): string {
  const p = new URLSearchParams();
  if (q) p.set("q", q);
  if (tab !== "all") p.set("tab", tab);
  const qs = p.toString();
  return `/search${qs ? `?${qs}` : ""}`;
}

/**
 * Search page layout
 * ------------------
 * On the "All" tab we render a Spotify-style hero row at the top:
 * Top Result (Tidal-nominated best match) on the left, Songs column on
 * the right. Below the hero, Artists / Albums / Playlists each show
 * one row of cards (clipped to whatever fits the viewport) with a
 * "View more" link that flips the tab. The hero hides when the user
 * picks a specific tab — that view is meant to be a single dense list
 * of one kind, not a curated landing.
 *
 * The tab itself lives in the URL (`?tab=…`) so clicking "View more"
 * is a real navigation, the tab survives a refresh, and the back
 * button works the way users expect.
 */

export function Search({ onDownload }: { onDownload: OnDownload }) {
  // The query and tab both live in the URL so the NavBar's search
  // input, the View-more links, and this page stay in sync. Typing
  // into the NavBar updates ?q=<value>; clicking a tab or View-more
  // updates ?tab=<filter>.
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const filter = asFilter(params.get("tab"));
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [format, setFormat] = useState<AudioFormat>("all");
  const debounceRef = useRef<number | null>(null);
  const cols = useColumnCount();

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

  const onTabChange = (next: Filter) => {
    const p = new URLSearchParams(params);
    if (next === "all") p.delete("tab");
    else p.set("tab", next);
    // Replace, not push — clicking through tabs shouldn't pollute
    // history with one entry per tab the way an explicit nav would.
    setParams(p, { replace: true });
  };

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
  const showFormatFilter =
    !!results &&
    (showAll || filter === "tracks" || filter === "albums") &&
    hasAnyFormatTags([...results.tracks, ...results.albums]);

  // Hero row only appears on the "All" tab. The Songs-column on the
  // right is a compact slice of the track results so the user can
  // peek the top tracks without scrolling. Capped at 5 — the hero
  // top-hit card is just tall enough to balance five compact track
  // rows against, and five reads as a nicer "top results" set than
  // the four it used to be.
  //
  // When the top hit is itself a track, drop it from the Songs column
  // so the same row doesn't render twice — the hero already shows it.
  const topHitTrackId = topHit?.kind === "track" ? topHit.id : null;
  const tracksWithoutTopHit = topHitTrackId
    ? filteredTracks.filter((t) => t.id !== topHitTrackId)
    : filteredTracks;
  const heroTracks = tracksWithoutTopHit.slice(0, 5);
  const showHero = showAll && (topHit !== null || heroTracks.length > 0);

  return (
    <div>
      {loading && (
        <div className="mb-4 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Searching…
        </div>
      )}

      {results && hasAny && (
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <Tabs value={filter} onValueChange={(v) => onTabChange(v as Filter)}>
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
              <div className="mb-4 mt-8 flex items-baseline justify-between gap-4">
                <h2 className="text-2xl font-bold tracking-tight">Songs</h2>
                {tracksWithoutTopHit.length > heroTracks.length && (
                  <ViewMoreLink to={tabUrl(q, "tracks")} />
                )}
              </div>
              <TrackList tracks={heroTracks} onDownload={onDownload} />
            </div>
          )}
        </div>
      )}

      {/* Dedicated Tracks tab — full TrackList. (On the "All" tab the
          tracks live inside the hero's Songs column instead.) */}
      {filter === "tracks" && filteredTracks.length > 0 && (
        <>
          <SectionHeader title="Tracks" />
          <TrackList tracks={filteredTracks} onDownload={onDownload} />
        </>
      )}

      {/* Per-type rows. On the "All" tab each section is one viewport
          row + a "View more" link. On a dedicated tab the same data
          renders as a full grid with no link (the user is already
          drilled in). */}
      {results && results.artists.length > 0 && showAll && (
        <MediaRow
          title="Artists"
          items={results.artists}
          cols={cols}
          viewMoreTo={tabUrl(q, "artists")}
        />
      )}
      {filter === "artists" && results && results.artists.length > 0 && (
        <>
          <SectionHeader title="Artists" />
          <Grid>
            {results.artists.map((a) => (
              <MediaCard key={a.id} item={a} />
            ))}
          </Grid>
        </>
      )}

      {filteredAlbums.length > 0 && showAll && (
        <MediaRow
          title="Albums"
          items={filteredAlbums}
          cols={cols}
          viewMoreTo={tabUrl(q, "albums")}
          onDownload={onDownload}
        />
      )}
      {filter === "albums" && filteredAlbums.length > 0 && (
        <>
          <SectionHeader title="Albums" />
          <Grid>
            {filteredAlbums.map((a) => (
              <MediaCard key={a.id} item={a} onDownload={onDownload} />
            ))}
          </Grid>
        </>
      )}

      {results && results.playlists.length > 0 && showAll && (
        <MediaRow
          title="Playlists"
          items={results.playlists}
          cols={cols}
          viewMoreTo={tabUrl(q, "playlists")}
          onDownload={onDownload}
        />
      )}
      {filter === "playlists" && results && results.playlists.length > 0 && (
        <>
          <SectionHeader title="Playlists" />
          <Grid>
            {results.playlists.map((p) => (
              <MediaCard key={p.id} item={p} onDownload={onDownload} />
            ))}
          </Grid>
        </>
      )}
    </div>
  );
}

/**
 * One row of MediaCards clipped to the current breakpoint's column
 * count, with a "View more" link in the header that drills into the
 * dedicated tab. Mirrors the convention used on Home, Album,
 * ArtistDetail, and Stats.
 */
function MediaRow<T extends Album | Artist | Playlist>({
  title,
  items,
  cols,
  viewMoreTo,
  onDownload,
}: {
  title: string;
  items: T[];
  cols: number;
  viewMoreTo: string;
  onDownload?: OnDownload;
}) {
  const visible = items.slice(0, cols);
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
