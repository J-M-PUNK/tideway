import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import type { AotyAlbum, Album } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { api } from "@/api/client";
import { Grid } from "@/components/Grid";
import { GridSkeleton } from "@/components/Skeletons";
import { ErrorView } from "@/components/ErrorView";
import { AotyCard, AotyAttribution } from "@/components/AotyHomeSection";

type GenreOpt = { slug: string; name: string };

/**
 * Full-grid drill-down for the AOTY rows on Home. Both routes have a
 * genre picker that fetches that genre's real AOTY chart (not a
 * client-side filter of the global list — that left niche genres
 * with a handful of entries):
 *   - top-of-year: picker options come from the genre tags on the
 *     year's chart rows; selecting one loads /genre/{slug}/{year}/.
 *   - new-releases: options come from AOTY's genre index; selecting
 *     one loads that genre's "Recent" section.
 */
export function AotyDrilldownPage() {
  const { section = "top-of-year" } = useParams();
  const config = useMemo(
    () => SECTIONS[section] ?? SECTIONS["top-of-year"],
    [section],
  );

  // Selected AOTY genre slug; "" = the section's default listing.
  const [genre, setGenre] = useState("");
  // Drop a stale selection when navigating between AOTY sections —
  // the same component instance is reused across the routes.
  useEffect(() => setGenre(""), [section]);

  // Unfiltered listing: the grid when no genre is picked, and (for
  // top-of-year) the source of the picker's options. Server-cached,
  // so re-requesting it while a genre is selected is cheap.
  const base = useApi(() => config.defaultFetch(), [section]);
  // Explicit option source (new-releases uses AOTY's genre index).
  const explicitOpts = useApi<GenreOpt[]>(
    () =>
      config.optionsSource === "listing"
        ? Promise.resolve([])
        : config.optionsSource(),
    [section],
  );
  // Grid data for a selected genre. Null resolver when none picked so
  // the hook stays unconditional without firing a needless request.
  const sel = useApi<AotyAlbum[] | null>(
    () => (genre ? config.genreFetch(genre) : Promise.resolve(null)),
    [section, genre],
  );

  const loading = base.loading || (!!genre && sel.loading);
  const error = base.error || (genre ? sel.error : null);

  const options: GenreOpt[] = useMemo(() => {
    if (config.optionsSource !== "listing") return explicitOpts.data ?? [];
    const m = new Map<string, string>();
    for (const e of base.data ?? []) {
      e.genre_slugs.forEach((slug, i) => {
        const name = e.genres[i];
        if (slug && name && !m.has(slug)) m.set(slug, name);
      });
    }
    return [...m]
      .map(([slug, name]) => ({ slug, name }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [config.optionsSource, explicitOpts.data, base.data]);

  if (loading) {
    return (
      <div>
        <div className="mb-8">
          <h1 className="text-3xl font-bold tracking-tight">{config.title}</h1>
          <AotyAttribution />
        </div>
        <GridSkeleton count={18} />
      </div>
    );
  }
  if (error) return <ErrorView error={error} />;

  const listing = genre ? (sel.data ?? []) : (base.data ?? []);
  const filtered = listing.filter(
    (e): e is AotyAlbum & { tidal_album: Album } => e.tidal_album !== null,
  );
  const playable = config.sortByScore
    ? [...filtered].sort((a, b) => (b.score ?? -1) - (a.score ?? -1))
    : filtered;

  return (
    <div>
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
      {playable.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {genre
            ? "No matching albums right now."
            : "Nothing to show right now."}
        </p>
      ) : (
        <Grid>
          {playable.map((entry) => (
            <AotyCard key={entry.tidal_album.id} entry={entry} />
          ))}
        </Grid>
      )}
    </div>
  );
}

// Per-section configuration. Adding a new AOTY drill-down is one
// entry here plus a route in App.tsx.
type SectionConfig = {
  title: string;
  sortByScore: boolean;
  allLabel: string;
  /** Unfiltered listing: grid when no genre, and (for "listing"
   *  options) the source of the picker. */
  defaultFetch: () => Promise<AotyAlbum[]>;
  /** Listing for a selected genre slug. */
  genreFetch: (slug: string) => Promise<AotyAlbum[]>;
  /** "listing" derives {slug,name} from defaultFetch's genre tags;
   *  a function supplies them explicitly. */
  optionsSource: "listing" | (() => Promise<GenreOpt[]>);
};

const SECTIONS: Record<string, SectionConfig> = {
  "top-of-year": {
    title: `Top albums of ${new Date().getFullYear()}`,
    sortByScore: false,
    allLabel: "All genres",
    defaultFetch: () => api.aoty.topOfYear({ limit: 100 }),
    genreFetch: (slug) => api.aoty.topOfYear({ genre: slug, limit: 60 }),
    optionsSource: "listing",
  },
  "new-releases": {
    title: "New album releases",
    sortByScore: false,
    allLabel: "This week",
    defaultFetch: () => api.aoty.recentReleases(100),
    genreFetch: (slug) => api.aoty.genreReleases(slug),
    optionsSource: () => api.aoty.genres(),
  },
};
