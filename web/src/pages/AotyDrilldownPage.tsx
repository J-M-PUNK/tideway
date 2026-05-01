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
    () => SECTIONS[section as keyof typeof SECTIONS] ?? SECTIONS["top-of-year"],
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

  const playable = (data ?? []).filter(
    (e): e is AotyAlbum & { tidal_album: Album } => e.tidal_album !== null,
  );
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
const SECTIONS = {
  "top-of-year": {
    title: `Top albums of ${new Date().getFullYear()}`,
    fetch: () => api.aoty.topOfYear({ limit: 100 }),
  },
  "new-releases": {
    title: "New releases",
    fetch: () => api.aoty.recentReleases(60),
  },
} as const;
