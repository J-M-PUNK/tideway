import { useParams } from "react-router-dom";
import { Compass } from "lucide-react";
import { api } from "@/api/client";
import { useApi } from "@/hooks/useApi";
import { Grid } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

// The drill-down shows the rest of the row's already-resolved pool beyond
// the shelf — matched to the server's _SECTION_RESOLVE_POOL, so "show
// more" costs no extra Tidal resolution.
const DRILLDOWN_LIMIT = 36;

/**
 * Generic "show more" drill-down behind a For You row (#307). Renders the
 * row as a full wrapping grid instead of a horizontal shelf, showing more
 * of the albums the shelf capped off. "From Your Genres" has its own
 * richer per-genre drill-down; this backs New Releases, Popular in Your
 * Orbit, and Fans Also Like.
 */
export function ForYouSectionPage() {
  const { key = "" } = useParams();
  const { data, loading, error } = useApi(
    () => api.recommendationSection(key, DRILLDOWN_LIMIT),
    [key],
  );

  if (loading) return <GridSkeleton count={18} />;
  if (error) return <ErrorView error={error} />;

  const section = data?.section ?? null;
  if (!section || section.albums.length === 0) {
    return (
      <EmptyState
        icon={Compass}
        title="Nothing more to show here"
        description="This row doesn't have more picks right now — check back as your listening grows."
      />
    );
  }

  return (
    <div>
      <div className="mb-6 mt-2">
        <h1 className="text-2xl font-bold tracking-tight">{section.title}</h1>
        {section.subtitle && (
          <p className="mt-1 text-sm text-muted-foreground">
            {section.subtitle}
          </p>
        )}
      </div>
      <Grid>
        {section.albums.map((album) => (
          <MediaCard key={album.id} item={album} />
        ))}
      </Grid>
    </div>
  );
}
