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

type RecAlbum = Album & { reason?: string };
type Section = {
  key: string;
  title: string;
  subtitle: string;
  albums: RecAlbum[];
};

/**
 * For You (#307) — a sectioned discovery page rather than one flat list.
 * The backend returns titled rows (Made For You, New Releases For You,
 * per-genre "Best of…", Popular in Your Orbit, and Tidal's "Because you
 * listened to X" modules), each already ranked; this page just lays them
 * out as horizontal shelves.
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

  const sections = data.sections.filter((s) => s.albums.length > 0);
  if (sections.length === 0) {
    return (
      <div>
        <PageHeading />
        <EmptyState
          icon={Compass}
          title="Not enough to go on yet"
          description="Favorite a few albums or artists in Tidal — we build recommendations from what you already like. Connecting Last.fm in Settings makes them sharper and unlocks genre rows."
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
      <div className="flex flex-col gap-10">
        {sections.map((section) => (
          <Shelf key={section.key} section={section} onDownload={onDownload} />
        ))}
      </div>
    </div>
  );
}

function PageHeading() {
  return (
    <div className="mb-8">
      <h2 className="mb-1 flex items-center gap-2 text-xl font-bold tracking-tight">
        <Sparkles className="h-5 w-5 text-primary" /> For You
      </h2>
      <p className="text-sm text-muted-foreground">
        Discovery built from your favorites, listening history, new releases,
        and your genres.
      </p>
    </div>
  );
}

function Shelf({
  section,
  onDownload,
}: {
  section: Section;
  onDownload: OnDownload;
}) {
  return (
    <section>
      <h3 className="text-lg font-bold tracking-tight">{section.title}</h3>
      {section.subtitle && (
        <p className="mb-3 text-xs text-muted-foreground">{section.subtitle}</p>
      )}
      {!section.subtitle && <div className="mb-3" />}
      <div className="flex gap-4 overflow-x-auto pb-2 scrollbar-thin">
        {section.albums.map((item) => (
          <RecCard key={item.id} item={item} onDownload={onDownload} />
        ))}
      </div>
    </section>
  );
}

function RecCard({
  item,
  onDownload,
}: {
  item: RecAlbum;
  onDownload: OnDownload;
}) {
  const cover = imageProxy(item.cover);
  const artist = item.artists?.map((a) => a.name).join(", ") ?? "";
  return (
    <div className="group relative flex w-44 shrink-0 flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent">
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
          {item.reason && (
            <div
              className="mt-1 line-clamp-1 text-[11px] text-primary/80"
              title={item.reason}
            >
              {item.reason}
            </div>
          )}
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
