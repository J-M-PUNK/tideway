import { Link } from "react-router-dom";
import { Music, Rss } from "lucide-react";
import { api } from "@/api/client";
import type { Album } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { DownloadButton } from "@/components/DownloadButton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { PageView } from "@/components/PageView";
import { GridSkeleton } from "@/components/Skeletons";
import { imageProxy } from "@/lib/utils";

type FeedItem = Album & { released_at: string };

/**
 * Feed — recent releases from artists the user has favorited or watched.
 * Mirrors Tidal's "What's new" surface, trimmed to the part that's useful
 * for a download client: album tiles sorted by release date.
 */
export function FeedPage({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.feed(), []);

  if (loading) {
    return (
      <div>
        <Header />
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load feed"} />;

  const items = data.items;
  const editorial = data.editorial;
  const hasCurated = items.length > 0;
  const hasEditorial =
    !!editorial && Array.isArray(editorial.categories) && editorial.categories.length > 0;

  if (!hasCurated && !hasEditorial) {
    return (
      <div>
        <Header />
        <EmptyState
          icon={Music}
          title="No new releases yet"
          description="Favorite or watch some artists in Tidal — their new albums will show up here as they drop."
        />
      </div>
    );
  }

  const groups = hasCurated ? groupByDay(items) : [];

  return (
    <div>
      <Header />
      {hasCurated && (
        <div className="mb-12 flex flex-col gap-10">
          <div>
            <h2 className="mb-1 text-xl font-bold tracking-tight">
              New from your artists
            </h2>
            <p className="mb-6 text-sm text-muted-foreground">
              Releases from the artists you follow and watch.
            </p>
            <div className="flex flex-col gap-10">
              {groups.map((group) => (
                <section key={group.label}>
                  <h3 className="mb-4 text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                    {group.label}
                  </h3>
                  <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
                    {group.items.map((item) => (
                      <FeedCard key={item.id} item={item} onDownload={onDownload} />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </div>
        </div>
      )}

      {hasEditorial && editorial && (
        <div>
          {hasCurated && (
            <h2 className="mb-6 text-xl font-bold tracking-tight">For you</h2>
          )}
          <PageView page={editorial} onDownload={onDownload} />
        </div>
      )}
    </div>
  );
}

function Header() {
  return (
    <div className="mb-8">
      <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
        <Rss className="h-7 w-7" /> Feed
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        New releases from your favorite and watched artists.
      </p>
    </div>
  );
}

function FeedCard({ item, onDownload }: { item: FeedItem; onDownload: OnDownload }) {
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
          <div className="mt-0.5 text-[11px] text-muted-foreground/70">
            {formatRelative(item.released_at)}
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

function groupByDay(items: FeedItem[]): { label: string; items: FeedItem[] }[] {
  const buckets = new Map<string, { label: string; sort: number; items: FeedItem[] }>();
  for (const item of items) {
    const key = item.released_at.slice(0, 10); // YYYY-MM-DD
    const existing = buckets.get(key);
    if (existing) {
      existing.items.push(item);
      continue;
    }
    buckets.set(key, {
      label: formatGroupLabel(item.released_at),
      sort: Date.parse(key) || 0,
      items: [item],
    });
  }
  return Array.from(buckets.values()).sort((a, b) => b.sort - a.sort);
}

function formatGroupLabel(iso: string): string {
  const date = new Date(iso);
  if (isNaN(date.getTime())) return "Recent";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const dayOf = new Date(date);
  dayOf.setHours(0, 0, 0, 0);
  const diffDays = Math.round((today.getTime() - dayOf.getTime()) / (1000 * 60 * 60 * 24));
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays} days ago`;
  return date.toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

function formatRelative(iso: string): string {
  const date = new Date(iso);
  if (isNaN(date.getTime())) return "";
  const diffMs = Date.now() - date.getTime();
  const days = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks} week${weeks === 1 ? "" : "s"} ago`;
  const months = Math.floor(days / 30);
  return `${months} month${months === 1 ? "" : "s"} ago`;
}
