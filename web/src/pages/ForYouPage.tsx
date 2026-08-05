import { Link } from "react-router-dom";
import { Compass, Music, Sparkles } from "lucide-react";
import { api } from "@/api/client";
import type { Album } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { queryKeys } from "@/api/queryKeys";
import { Button } from "@/components/ui/button";
import { DownloadButton } from "@/components/DownloadButton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";
import { imageProxy } from "@/lib/utils";

type Recommended = Album & { reason: string };

/**
 * For You (#307) — taste-based album recommendations. The backend blends
 * Tidal's own similar-album/artist graph with Last.fm similar artists
 * (when connected) and attaches a "Because you like X" reason to each
 * pick. This page just renders the ranked list; the ranking lives in
 * `/api/recommendations/albums`.
 */
export function ForYouPage({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.recommendations(), [], {
    cacheKey: queryKeys.recommendations,
  });

  if (loading) {
    return (
      <div>
        <PageHeading />
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data)
    return <ErrorView error={error ?? "Couldn't load recommendations"} />;

  // Setting turned off — the surface exists but the user opted out.
  if (!data.enabled) {
    return (
      <div>
        <PageHeading />
        <EmptyState
          icon={Sparkles}
          title="Recommendations are turned off"
          description="Turn them back on in Settings to get album picks based on your taste."
          action={
            <Button asChild variant="secondary" size="sm">
              <Link to="/settings">Open Settings</Link>
            </Button>
          }
        />
      </div>
    );
  }

  if (data.albums.length === 0) {
    return (
      <div>
        <PageHeading />
        <EmptyState
          icon={Compass}
          title="Not enough to go on yet"
          description="Favorite a few albums or artists in Tidal — we build recommendations from what you already like. Connecting Last.fm in Settings makes them sharper."
          action={
            <Button asChild variant="secondary" size="sm">
              <Link to="/library/artists">Find artists to follow</Link>
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div>
      <PageHeading />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
        {data.albums.map((item) => (
          <RecCard key={item.id} item={item} onDownload={onDownload} />
        ))}
      </div>
    </div>
  );
}

function PageHeading() {
  return (
    <div className="mb-6">
      <h2 className="mb-1 flex items-center gap-2 text-xl font-bold tracking-tight">
        <Sparkles className="h-5 w-5 text-primary" /> For You
      </h2>
      <p className="text-sm text-muted-foreground">
        Albums picked from your favorites and listening — every one tells you
        why it's here.
      </p>
    </div>
  );
}

function RecCard({
  item,
  onDownload,
}: {
  item: Recommended;
  onDownload: OnDownload;
}) {
  const cover = imageProxy(item.cover);
  const artist = item.artists?.map((a) => a.name).join(", ") ?? "";
  return (
    <div className="group relative flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent">
      <Link to={`/album/${item.id}`} className="flex flex-col gap-3">
        <div className="aspect-square overflow-hidden rounded-md bg-secondary">
          {cover ? (
            <img
              src={cover}
              alt=""
              className="h-full w-full object-cover transition-transform group-hover:scale-105"
              loading="lazy"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Music className="h-10 w-10" />
            </div>
          )}
        </div>
        <div className="min-w-0">
          <div className="truncate font-semibold">{item.name}</div>
          <div className="truncate text-xs text-muted-foreground">{artist}</div>
          <div
            className="mt-1 line-clamp-1 text-[11px] text-primary/80"
            title={item.reason}
          >
            {item.reason}
          </div>
        </div>
      </Link>
      <div className="absolute right-3 top-3 opacity-0 transition-opacity group-hover:opacity-100">
        <DownloadButton
          kind="album"
          id={item.id}
          onPick={onDownload}
          iconOnly
          variant="secondary"
          size="sm"
        />
      </div>
    </div>
  );
}
