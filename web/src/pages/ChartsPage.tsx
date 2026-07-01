import { useParams } from "react-router-dom";
import { Newspaper } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { queryKeys } from "@/api/queryKeys";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

/**
 * New Releases page. Maps to Tidal's editorial page path and renders
 * through the generic PageView so layout stays consistent with Explore.
 * (The former Top / Rising tabs were removed — the useful discovery
 * surface is the Last.fm-backed Popular page on its own route.)
 */
type ChartKey = "new";

interface ChartSpec {
  title: string;
  icon: LucideIcon;
  path: string;
}

const CHARTS: Record<ChartKey, ChartSpec> = {
  new: {
    title: "New Releases",
    icon: Newspaper,
    path: "pages/explore_new_music",
  },
};

export function ChartsPage({ onDownload }: { onDownload: OnDownload }) {
  const { chart = "new" } = useParams<{ chart: ChartKey }>();
  const spec = CHARTS[chart as ChartKey] ?? CHARTS.new;
  const { data, loading, error } = useApi(
    () => api.pagePath(spec.path),
    [spec.path],
    { cacheKey: queryKeys.charts(spec.path) },
  );

  if (loading) {
    return (
      <div>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data)
    return <ErrorView error={error ?? `Couldn't load ${spec.title}`} />;

  return (
    <div>
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}
