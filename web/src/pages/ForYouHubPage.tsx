import { useCallback, useState } from "react";
import { Compass } from "lucide-react";
import { api } from "@/api/client";
import { useApi } from "@/hooks/useApi";
import { RecShelf, type RecSection } from "@/components/RecShelf";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

/**
 * Drill-down behind a For You row (#307). The same shelves as the main
 * page, unblended: "From Your Genres" becomes one row per genre, each
 * paging further into that genre's canon on demand.
 */
function ForYouHubPage({
  title,
  description,
  fetch,
}: {
  title: string;
  description: string;
  fetch: () => Promise<{ enabled: boolean; sections: RecSection[] }>;
}) {
  const { data, loading, error } = useApi(fetch, []);
  // Only the pages the user has loaded live in state; the first page
  // stays owned by the fetch. Deriving the rendered rows from both means
  // no effect has to copy fetched data into state and re-sync it.
  const [extra, setExtra] = useState<
    Record<string, { albums: RecSection["albums"]; has_more: boolean }>
  >({});
  const [busy, setBusy] = useState<string | null>(null);

  const rows = (data?.sections ?? [])
    .filter((s) => s.albums.length > 0)
    .map((s) => {
      const more = extra[s.key];
      if (!more) return s;
      return {
        ...s,
        albums: [...s.albums, ...more.albums],
        has_more: more.has_more,
      };
    });

  const loadMore = useCallback(async (section: RecSection) => {
    if (!section.slug) return;
    setBusy(section.key);
    try {
      const res = await api.recommendationGenrePage(
        section.slug,
        section.albums.length,
      );
      const page = res.section;
      if (!page) return;
      setExtra((prev) => {
        const seenIds = new Set(section.albums.map((a) => a.id));
        const held = prev[section.key]?.albums ?? [];
        // A window can repeat an album the previous one already rendered
        // (AOTY paginates its listing; we render whatever resolves), so
        // merge on id rather than concatenating blindly.
        const added = page.albums.filter((a) => !seenIds.has(a.id));
        return {
          ...prev,
          [section.key]: {
            albums: [...held, ...added],
            has_more: page.has_more ?? false,
          },
        };
      });
    } catch {
      // Leave the row as-is; the button stays available to retry.
    } finally {
      setBusy(null);
    }
  }, []);

  return (
    <div>
      <div className="mb-8">
        <h2 className="text-xl font-bold tracking-tight">{title}</h2>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>

      {loading && <GridSkeleton count={12} />}
      {!loading && (error || !data) && (
        <ErrorView error={error ?? "Couldn't load recommendations"} />
      )}
      {!loading && data && rows.length === 0 && (
        <EmptyState
          icon={Compass}
          title="Nothing here yet"
          description="Listen to a bit more and this fills in — it's built from the genres and artists you actually play."
        />
      )}
      {!loading && rows.length > 0 && (
        <div className="flex flex-col gap-10">
          {rows.map((s) => (
            <RecShelf
              key={s.key}
              section={s}
              onLoadMore={s.has_more ? () => void loadMore(s) : undefined}
              loadingMore={busy === s.key}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function ForYouGenresPage() {
  return (
    <ForYouHubPage
      title="From Your Genres"
      description="The highest rated albums in each genre you listen to."
      fetch={() => api.recommendationGenres()}
    />
  );
}
