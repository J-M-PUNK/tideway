import { Link } from "react-router-dom";
import { Sparkles } from "lucide-react";
import { api } from "@/api/client";
import { useApi } from "@/hooks/useApi";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";
import { imageProxy } from "@/lib/utils";

/**
 * Full grid of the user's Tidal mixes (Daily Mix 1/2/3, Discovery Mix,
 * etc.). Home shows a single row + "View more" link that lands here.
 */
export function MixesPage() {
  const { data: mixes, loading, error } = useApi(() => api.mixes(), []);

  if (loading) {
    return (
      <div>
        <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
          <Sparkles className="h-7 w-7 text-primary" /> Made for you
        </h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !mixes)
    return <ErrorView error={error ?? "Couldn't load mixes"} />;

  return (
    <div>
      <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
        <Sparkles className="h-7 w-7 text-primary" /> Made for you
      </h1>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-5 xl:grid-cols-6">
        {mixes.map((m) => (
          <Link
            key={m.id}
            to={`/mix/${m.id}`}
            className="group flex flex-col gap-2 rounded-lg p-2 transition-colors hover:bg-accent"
          >
            <div className="aspect-square overflow-hidden rounded-md bg-secondary shadow">
              {m.cover ? (
                <img
                  src={imageProxy(m.cover)}
                  alt=""
                  className="h-full w-full object-cover transition-transform group-hover:scale-105"
                  loading="lazy"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center text-muted-foreground">
                  <Sparkles className="h-10 w-10" />
                </div>
              )}
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">{m.name}</div>
              {m.subtitle && (
                <div className="truncate text-xs text-muted-foreground">
                  {m.subtitle}
                </div>
              )}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
