import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import type { AotyAlbum, AotyPage, Album } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { useInfinitePages } from "@/hooks/useInfinitePages";
import { api } from "@/api/client";
import { Grid } from "@/components/Grid";
import { GridSkeleton } from "@/components/Skeletons";
import { ErrorView } from "@/components/ErrorView";
import { AotyCard, AotyAttribution } from "@/components/AotyHomeSection";

type GenreOpt = { slug: string; name: string };
const PAGE_SIZE = 18;

/**
 * Full-grid drill-down for the AOTY rows on Home, paginated with infinite
 * scroll (#perf): each request resolves only ~18 albums to Tidal instead
 * of all 100 up front, so the page paints fast and streams more as you
 * scroll. Both routes have a genre picker that loads that genre's real
 * AOTY chart (not a client-side filter, which left niche genres with a
 * handful of entries).
 */
export function AotyDrilldownPage() {
  const { section = "top-of-year" } = useParams();
  const config = useMemo(
    () => SECTIONS[section] ?? SECTIONS["top-of-year"],
    [section],
  );

  // Selected AOTY genre slug; "" = the section's default listing. The same
  // component instance is reused across routes, so drop a stale selection.
  const [genre, setGenre] = useState("");
  useEffect(() => setGenre(""), [section]);

  const {
    items,
    firstPage,
    hasMore,
    loading,
    loadingMore,
    error,
    sentinelRef,
  } = useInfinitePages<AotyAlbum, AotyPage>(
    (offset) => config.fetchPage(offset, PAGE_SIZE, genre),
    PAGE_SIZE,
    [section, genre],
  );

  // Genre picker options. New-releases pulls AOTY's genre index; top-of-
  // year reads them off the default listing's first page and keeps them
  // even after a genre is picked (that page no longer carries them).
  const genreIndex = useApi<GenreOpt[]>(
    () =>
      config.optionsFrom === "genres" ? api.aoty.genres() : Promise.resolve([]),
    [section],
  );
  const [listingGenres, setListingGenres] = useState<GenreOpt[]>([]);
  useEffect(() => setListingGenres([]), [section]);
  useEffect(() => {
    if (config.optionsFrom === "firstPage" && firstPage?.genres) {
      setListingGenres(firstPage.genres);
    }
  }, [config.optionsFrom, firstPage]);
  const options =
    config.optionsFrom === "genres" ? (genreIndex.data ?? []) : listingGenres;

  const header = (
    <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">{config.title}</h1>
        <AotyAttribution />
      </div>
      {options.length > 0 && (
        <select
          value={genre}
          onChange={(e) => setGenre(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm"
          aria-label="Filter by genre"
        >
          <option value="">{config.allLabel}</option>
          {options.map((g) => (
            <option key={g.slug} value={g.slug}>
              {g.name}
            </option>
          ))}
        </select>
      )}
    </div>
  );

  if (loading) {
    return (
      <div>
        {header}
        <GridSkeleton count={18} />
      </div>
    );
  }
  if (error) return <ErrorView error={error} />;

  const playable = items.filter(
    (e): e is AotyAlbum & { tidal_album: Album } => e.tidal_album !== null,
  );

  return (
    <div>
      {header}
      {playable.length === 0 && !hasMore ? (
        <p className="text-sm text-muted-foreground">
          {genre
            ? "No matching albums right now."
            : "Nothing to show right now."}
        </p>
      ) : (
        <>
          <Grid>
            {playable.map((entry) => (
              <AotyCard key={entry.tidal_album.id} entry={entry} />
            ))}
          </Grid>
          {hasMore && (
            <div ref={sentinelRef} className="mt-6" aria-hidden>
              <GridSkeleton count={6} />
            </div>
          )}
          {!hasMore && loadingMore && <GridSkeleton count={6} />}
        </>
      )}
    </div>
  );
}

// Per-section configuration. Adding a new AOTY drill-down is one entry
// here plus a route in App.tsx.
type SectionConfig = {
  title: string;
  allLabel: string;
  /** One page of the listing; `genre` "" means the section default. */
  fetchPage: (
    offset: number,
    limit: number,
    genre: string,
  ) => Promise<AotyPage>;
  /** Where the picker options come from: the default listing's first page
   *  (top-of-year) or AOTY's genre index (new-releases). */
  optionsFrom: "firstPage" | "genres";
};

const SECTIONS: Record<string, SectionConfig> = {
  "top-of-year": {
    title: `Top albums of ${new Date().getFullYear()}`,
    allLabel: "All genres",
    fetchPage: (offset, limit, genre) =>
      genre
        ? api.aoty.topOfYear({ genre, offset, limit })
        : api.aoty.topOfYear({ offset, limit }),
    optionsFrom: "firstPage",
  },
  "new-releases": {
    title: "New album releases",
    allLabel: "This week",
    fetchPage: (offset, limit, genre) =>
      genre
        ? api.aoty.genreReleases(genre, { offset, limit })
        : api.aoty.recentReleases({ offset, limit }),
    optionsFrom: "genres",
  },
};
