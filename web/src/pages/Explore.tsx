import { Compass } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

export function Explore({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.page("explore"), []);

  if (loading) {
    return (
      <div>
        <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
          <Compass className="h-7 w-7" /> Explore
        </h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load explore"} />;

  return (
    <div>
      <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
        <Compass className="h-7 w-7" /> Explore
      </h1>
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}
