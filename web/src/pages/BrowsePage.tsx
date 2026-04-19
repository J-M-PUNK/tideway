import { useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

/**
 * Drill-down page for any Tidal PageLink (`pages/genre_hip_hop` etc.) —
 * the Explore page's tiles route here with their `api_path` url-encoded
 * into the :path param.
 */
export function BrowsePage({ onDownload }: { onDownload: OnDownload }) {
  const { path = "" } = useParams();
  const decoded = decodeURIComponent(path);
  const { data, loading, error } = useApi(() => api.pagePath(decoded), [decoded]);

  const title = deriveTitle(decoded);

  if (loading) {
    return (
      <div>
        <h1 className="mb-8 text-3xl font-bold tracking-tight">{title}</h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load page"} />;

  return (
    <div>
      <h1 className="mb-8 text-3xl font-bold tracking-tight">{title}</h1>
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}

function deriveTitle(path: string): string {
  // e.g. "pages/genre_hip_hop" → "Genre Hip Hop" → "Hip Hop"
  const tail = path.replace(/^pages\//, "").replace(/^genre_/, "").replace(/^m_/, "");
  return tail
    .split("_")
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join(" ");
}
