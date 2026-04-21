import { useEffect, useRef, useState } from "react";
import { Loader2, Music, Search as SearchIcon } from "lucide-react";
import { api } from "@/api/client";
import type { SearchResponse } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Grid, SectionHeader } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { TrackList } from "@/components/TrackList";
import { EmptyState } from "@/components/EmptyState";
import {
  FormatFilter,
  type AudioFormat,
  hasAnyFormatTags,
  matchesFormat,
} from "@/components/FormatFilter";

type Filter = "all" | "tracks" | "albums" | "artists" | "playlists";

export function Search({ onDownload }: { onDownload: OnDownload }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [format, setFormat] = useState<AudioFormat>("all");
  const debounceRef = useRef<number | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Focus the search field on first mount (this is what Cmd/Ctrl+K navigates to).
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

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
      if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    };
  }, [q]);

  const hasAny =
    !!results &&
    (results.tracks.length > 0 ||
      results.albums.length > 0 ||
      results.artists.length > 0 ||
      results.playlists.length > 0);

  const filteredTracks =
    results && format !== "all"
      ? results.tracks.filter((t) => matchesFormat(t, format))
      : results?.tracks ?? [];
  const filteredAlbums =
    results && format !== "all"
      ? results.albums.filter((a) => matchesFormat(a, format))
      : results?.albums ?? [];
  const showTracks =
    results && (filter === "all" || filter === "tracks") && filteredTracks.length > 0;
  const showAlbums =
    results && (filter === "all" || filter === "albums") && filteredAlbums.length > 0;
  const showArtists =
    results && (filter === "all" || filter === "artists") && results.artists.length > 0;
  const showPlaylists =
    results && (filter === "all" || filter === "playlists") && results.playlists.length > 0;
  const showFormatFilter =
    !!results &&
    (filter === "all" || filter === "tracks" || filter === "albums") &&
    hasAnyFormatTags([...results.tracks, ...results.albums]);

  return (
    <div>
      <div className="relative mb-6 max-w-xl">
        <SearchIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="What do you want to listen to?"
          className="h-12 pl-10 text-base"
        />
        {loading && (
          <Loader2 className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-muted-foreground" />
        )}
      </div>

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
          description="Tracks, albums, artists, playlists — hit ⌘K from anywhere to get here fast."
        />
      )}

      {results && !hasAny && (
        <EmptyState icon={Music} title="No results" description={`Nothing matched "${q}".`} />
      )}

      {showTracks && (
        <>
          <SectionHeader title="Tracks" />
          <TrackList
            tracks={filteredTracks.slice(0, filter === "tracks" ? 999 : 6)}
            onDownload={onDownload}
          />
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
