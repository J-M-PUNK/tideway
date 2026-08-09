import { Link } from "react-router-dom";
import { Compass, Sparkles } from "lucide-react";
import { api } from "@/api/client";
import { useApi } from "@/hooks/useApi";
import { queryKeys } from "@/api/queryKeys";
import { Button } from "@/components/ui/button";
import { RecShelf } from "@/components/RecShelf";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

/**
 * For You (#307) — a sectioned discovery page rather than one flat list.
 * The backend returns titled rows, each already ranked; this page lays
 * them out as horizontal shelves. Rows the backend marks with a
 * `view_more` path get a drill-down link, the same pattern the AOTY rows
 * on Home use.
 *
 * No page heading: the sidebar already names the destination, and the
 * row titles carry the structure.
 */
export function ForYouPage() {
  const { data, loading, error } = useApi(() => api.recommendations(), [], {
    cacheKey: queryKeys.recommendations,
  });
  if (loading) return <GridSkeleton count={12} />;
  if (error || !data)
    return <ErrorView error={error ?? "Couldn't load recommendations"} />;

  if (!data.enabled) {
    return (
      <EmptyState
        icon={Sparkles}
        title="Recommendations are turned off"
        description="Turn them back on in Settings to get album picks based on your taste."
        action={
          <Button asChild variant="secondary" size="sm">
            <Link to="/settings">Open Settings</Link>
          </Button>
        }
      />
    );
  }

  const sections = data.sections.filter((s) => s.albums.length > 0);
  if (sections.length === 0) {
    return (
      <EmptyState
        icon={Compass}
        title="Not enough to go on yet"
        description="Favorite a few albums or artists in Tidal — we build recommendations from what you already like. Connecting Last.fm in Settings makes them sharper and unlocks genre rows."
        action={
          <Button asChild variant="secondary" size="sm">
            <Link to="/library/artists">Find artists to follow</Link>
          </Button>
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-10">
      {sections.map((section) => (
        <RecShelf key={section.key} section={section} />
      ))}
    </div>
  );
}
