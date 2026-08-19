import { Link } from "react-router-dom";
import { Compass, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RecShelf } from "@/components/RecShelf";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton, Skeleton } from "@/components/Skeletons";
import { useLazyRecommendations } from "@/hooks/useLazyRecommendations";

/**
 * For You (#307) — a sectioned discovery page rather than one flat list.
 *
 * The rows load lazily and in parallel: a cheap manifest paints the page
 * shell immediately, then each row's albums stream in from its own
 * request and pop in as they resolve, instead of one build-everything
 * call that made the whole page wait on the slowest section. The
 * "Recommended For You" blend is folded in on the client once every row
 * has settled, so the finished layout matches the old one-shot page.
 *
 * No page heading: the sidebar already names the destination, and the
 * row titles carry the structure.
 */
export function ForYouPage() {
  const recs = useLazyRecommendations();

  if (recs.status === "loading") return <GridSkeleton count={12} />;
  if (recs.status === "error") return <ErrorView error={recs.error} />;

  if (recs.status === "disabled") {
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

  if (recs.status === "empty") {
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
      {recs.items.map((item) =>
        item.kind === "section" ? (
          <RecShelf key={item.section.key} section={item.section} />
        ) : (
          <RecShelfSkeleton key={item.key} />
        ),
      )}
    </div>
  );
}

/** Placeholder for a row whose albums haven't arrived yet — a title bar
 *  plus a strip of card skeletons, matching a real horizontal shelf. */
function RecShelfSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-hidden>
      <Skeleton className="h-6 w-48" />
      <div className="flex gap-4 overflow-hidden">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="flex w-40 shrink-0 flex-col gap-2">
            <Skeleton className="aspect-square w-full rounded-lg" />
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        ))}
      </div>
    </div>
  );
}
