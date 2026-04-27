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
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data)
    return <ErrorView error={error ?? "Couldn't load explore"} />;

  return (
    <div>
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}
