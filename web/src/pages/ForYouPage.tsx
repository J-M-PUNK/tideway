import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Compass, Music, Sparkles, X } from "lucide-react";
import { api } from "@/api/client";
import type { Album } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { queryKeys } from "@/api/queryKeys";
import { Button } from "@/components/ui/button";
import { InlineHeart } from "@/components/MediaCard";
import { PlayMediaButton } from "@/components/PlayMediaButton";
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
export function ForYouPage() {
  const { data, loading, error } = useApi(() => api.recommendations(), [], {
    cacheKey: queryKeys.recommendations,
  });
  // Dismissed albums vanish immediately; the backend persists the choice
  // and down-weights the artist on the next (re)load.
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const onDismiss = useCallback((item: RecAlbum) => {
    setDismissed((prev) => new Set(prev).add(item.id));
    void api
      .dismissRecommendation(item.id, item.artists?.[0]?.name)
      .catch(() => {
        /* best-effort — it's gone from the view either way */
      });
  }, []);

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

  const sections = data.sections
    .map((s) => ({
      ...s,
      albums: s.albums.filter((a) => !dismissed.has(a.id)),
    }))
    .filter((s) => s.albums.length > 0);
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
          <Shelf key={section.key} section={section} onDismiss={onDismiss} />
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
  onDismiss,
}: {
  section: Section;
  onDismiss: (item: RecAlbum) => void;
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
          <RecCard key={item.id} item={item} onDismiss={onDismiss} />
        ))}
      </div>
    </section>
  );
}

function RecCard({
  item,
  onDismiss,
}: {
  item: RecAlbum;
  onDismiss: (item: RecAlbum) => void;
}) {
  const cover = imageProxy(item.cover);
  const artist = item.artists?.map((a) => a.name).join(", ") ?? "";
  // Same hover-reveal contract as MediaCard: play (bottom-left) and like
  // (bottom-right) sit on the cover and fade in on hover / keyboard focus.
  const hoverActions =
    "opacity-0 transition-all duration-200 ease-out group-hover:opacity-100 focus-within:opacity-100";
  return (
    <div className="group relative flex w-44 shrink-0 flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent">
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onDismiss(item);
        }}
        title="Not interested — hide this and show less like it"
        aria-label={`Not interested in ${item.name}`}
        className="absolute left-3 top-3 z-10 flex h-7 w-7 items-center justify-center rounded-full bg-black/70 text-white opacity-0 transition-opacity hover:bg-black/90 focus-visible:opacity-100 group-hover:opacity-100"
      >
        <X className="h-4 w-4" />
      </button>
      <Link to={`/album/${item.id}`} className="flex flex-col gap-3">
        <div className="relative aspect-square overflow-hidden rounded-md bg-secondary">
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
          <PlayMediaButton
            kind="album"
            id={item.id}
            className={`absolute bottom-2 left-2 h-10 w-10 ${hoverActions}`}
          />
          <InlineHeart
            kind="album"
            id={item.id}
            className={`absolute bottom-2 right-2 ${hoverActions}`}
          />
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
    </div>
  );
}
