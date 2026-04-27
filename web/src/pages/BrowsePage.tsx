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
  // decodeURIComponent throws URIError on malformed % escapes (e.g. a
  // stale bookmark or hand-crafted URL). Without this guard the whole
  // Shell above us would crash with no error boundary in its path.
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    decoded = path;
  }
  const { data, loading, error } = useApi(
    () => api.pagePath(decoded),
    [decoded],
  );

  // Prefer the title Tidal gives us in the response; fall back to a
  // title derived from the path only when the backend didn't include
  // one (older V1 pages).
  const title = data?.title || deriveTitle(decoded);

  if (loading) {
    return (
      <div>
        <h1 className="mb-8 text-3xl font-bold tracking-tight">
          {deriveTitle(decoded)}
        </h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data)
    return <ErrorView error={error ?? "Couldn't load page"} />;

  return (
    <div>
      <h1 className="mb-8 text-3xl font-bold tracking-tight">{title}</h1>
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}

function deriveTitle(path: string): string {
  // Fallback when the API response has no title. For V1 pages
  // ("pages/genre_hip_hop") this produces a readable "Hip Hop". For
  // V2 view-all paths ("home/pages/NEW_ALBUM_SUGGESTIONS/view-all")
  // we grab the middle segment since the prefix/suffix are noise.
  const viewAllMatch = path.match(/^home\/pages\/([^/]+)\/view-all$/i);
  const raw = viewAllMatch
    ? viewAllMatch[1]
    : path
        .replace(/^pages\//, "")
        .replace(/^genre_/, "")
        .replace(/^m_/, "");
  return raw
    .toLowerCase()
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join(" ");
}
