import { Smile } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";

export function MoodsPage({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.page("moods"), []);

  if (loading) {
    return (
      <div>
        <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
          <Smile className="h-7 w-7" /> Moods
        </h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load moods"} />;

  return (
    <div>
      <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
        <Smile className="h-7 w-7" /> Moods
      </h1>
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}
