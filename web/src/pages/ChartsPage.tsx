import { useParams } from "react-router-dom";
import { Flame, Newspaper, TrendingUp } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { PageView } from "@/components/PageView";
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
  subtitle: string;
  icon: LucideIcon;
  path: string;
}

const CHARTS: Record<ChartKey, ChartSpec> = {
  new: {
    title: "New Releases",
    subtitle: "Fresh albums and tracks Tidal's editors are highlighting.",
    icon: Newspaper,
    path: "pages/explore_new_music",
  },
  rising: {
    title: "Tidal Rising",
    subtitle: "Up-and-coming artists gaining traction.",
    icon: Flame,
    path: "pages/rising",
  },
  top: {
    title: "Top Charts",
    subtitle: "What's popular right now.",
    icon: TrendingUp,
    path: "pages/explore_top_music",
  },
};

export function ChartsPage({ onDownload }: { onDownload: OnDownload }) {
  const { chart = "new" } = useParams<{ chart: ChartKey }>();
  const spec = CHARTS[chart as ChartKey] ?? CHARTS.new;
  const { data, loading, error } = useApi(() => api.pagePath(spec.path), [spec.path]);

  const Icon = spec.icon;

  if (loading) {
    return (
      <div>
        <Header icon={Icon} title={spec.title} subtitle={spec.subtitle} />
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? `Couldn't load ${spec.title}`} />;

  return (
    <div>
      <Header icon={Icon} title={spec.title} subtitle={spec.subtitle} />
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}

function Header({
  icon: Icon,
  title,
  subtitle,
}: {
  icon: LucideIcon;
  title: string;
  subtitle: string;
}) {
  return (
    <div className="mb-8">
      <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
        <Icon className="h-7 w-7" /> {title}
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
    </div>
  );
}
