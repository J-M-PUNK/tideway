import { useMemo } from "react";
import { useParams } from "react-router-dom";
import type { AotyAlbum, Album } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { api } from "@/api/client";
import { Grid } from "@/components/Grid";
import { GridSkeleton } from "@/components/Skeletons";
import { ErrorView } from "@/components/ErrorView";
import { AotyCard } from "@/components/AotyHomeSection";

/**
 * Full-grid drill-down for the AOTY rows surfaced on the Home page.
 * Two routes share this page (top-of-year, new-releases) — they
 * differ only in the API call and the page title.
 *
 * The home-page rows show a single row of cards capped at the
 * viewport's column count; this page shows the full set as a
 * responsive grid. Same MediaCard styling as the rest of the app.
 */
export function AotyDrilldownPage() {
  const { section = "top-of-year" } = useParams();
  const config = useMemo(
    () => SECTIONS[section] ?? SECTIONS["top-of-year"],
    [section],
  );

  const { data, loading, error } = useApi(config.fetch, [section]);

  if (loading) {
    return (
      <div>
        <h1 className="mb-8 text-3xl font-bold tracking-tight">
          {config.title}
        </h1>
        <GridSkeleton count={18} />
      </div>
    );
  }
  if (error) return <ErrorView error={error} />;

  const filtered = (data ?? []).filter(
    (e): e is AotyAlbum & { tidal_album: Album } => e.tidal_album !== null,
  );
  // Sort by AOTY score (highest first) when the section is
  // configured for it. Entries without a score fall to the end.
  // Done in-place at render time rather than baked into the API
  // response so the same endpoint can serve both ordered views.
  const playable = config.sortByScore
    ? [...filtered].sort((a, b) => (b.score ?? -1) - (a.score ?? -1))
    : filtered;
  if (playable.length === 0) {
    return (
      <div>
        <h1 className="mb-8 text-3xl font-bold tracking-tight">
          {config.title}
        </h1>
        <p className="text-sm text-muted-foreground">
          Nothing to show right now.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1 className="mb-8 text-3xl font-bold tracking-tight">{config.title}</h1>
      <Grid>
        {playable.map((entry) => (
          <AotyCard key={entry.tidal_album.id} entry={entry} />
        ))}
      </Grid>
    </div>
  );
}

// Per-section configuration. Adding a new AOTY drill-down is one
// entry here plus a route in App.tsx.
type SectionConfig = {
  title: string;
  fetch: () => Promise<AotyAlbum[]>;
  sortByScore: boolean;
};

const SECTIONS: Record<string, SectionConfig> = {
  "top-of-year": {
    title: `Top albums of ${new Date().getFullYear()}`,
    fetch: () => api.aoty.topOfYear({ limit: 100 }),
    sortByScore: false,
  },
  "new-releases": {
    title: "New album releases",
    fetch: () => api.aoty.recentReleases(100),
    sortByScore: false,
  },
};
