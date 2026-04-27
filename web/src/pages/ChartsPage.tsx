import { useParams } from "react-router-dom";
import { Flame, Newspaper, TrendingUp } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { PageView } from "@/components/PageView";
import { ChartsNav } from "@/components/ChartsNav";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

/**
 * Dedicated Rising / New / Top pages. Each maps to one of Tidal's editorial
 * page paths and renders through the generic PageView so layout stays
 * consistent with Explore.
 */
type ChartKey = "new" | "rising" | "top";

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
  rising: {
    title: "Tidal Rising",
    icon: Flame,
    path: "pages/rising",
  },
  top: {
    title: "Top Charts",
    icon: TrendingUp,
    path: "pages/explore_top_music",
  },
};

export function ChartsPage({ onDownload }: { onDownload: OnDownload }) {
  const { chart = "new" } = useParams<{ chart: ChartKey }>();
  const spec = CHARTS[chart as ChartKey] ?? CHARTS.new;
  const { data, loading, error } = useApi(
    () => api.pagePath(spec.path),
    [spec.path],
  );

  // New Releases stays standalone; only Top and Rising share the
  // Charts tab strip (Popular lives on its own route).
  const showChartsNav = chart === "top" || chart === "rising";

  if (loading) {
    return (
      <div>
        {showChartsNav && <ChartsNav />}
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data)
    return <ErrorView error={error ?? `Couldn't load ${spec.title}`} />;

  return (
    <div>
      {showChartsNav && <ChartsNav />}
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}
