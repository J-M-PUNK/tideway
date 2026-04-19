import { Link } from "react-router-dom";
import { Music } from "lucide-react";
import type {
  Album,
  Artist,
  MixItem,
  PageCategory,
  PageItem,
  PageLinkItem,
  Playlist,
  TidalPage,
  Track,
} from "@/api/types";
import type { OnDownload } from "@/api/download";
import { MediaCard } from "@/components/MediaCard";
import { TrackList } from "@/components/TrackList";
import { cn, imageProxy } from "@/lib/utils";

interface Props {
  page: TidalPage;
  onDownload: OnDownload;
}

/**
 * Renders a Tidal editorial page (home, explore, drill-down) as a stack
 * of rows. Each row's layout is chosen from its type:
 *   - HorizontalList / ShortcutList / FeaturedItems / ItemList → grid of cards
 *   - TrackList → TrackList component
 *   - PageLinks → pill-grid of clickable category tiles
 */
export function PageView({ page, onDownload }: Props) {
  if (page.categories.length === 0) {
    return <div className="py-12 text-center text-sm text-muted-foreground">Nothing here.</div>;
  }
  return (
    <div className="flex flex-col gap-8">
      {page.categories.map((cat, i) => (
        <Section key={`${cat.type}-${i}`} category={cat} onDownload={onDownload} />
      ))}
    </div>
  );
}

function Section({
  category,
  onDownload,
}: {
  category: PageCategory;
  onDownload: OnDownload;
}) {
  const { type, title, subtitle, items } = category;

  if (type === "TrackList") {
    return (
      <div>
        {title && <SectionHeader title={title} subtitle={subtitle} />}
        <TrackList
          tracks={items.filter((i): i is Track => i.kind === "track")}
          onDownload={onDownload}

        />
      </div>
    );
  }

  if (type === "PageLinks") {
    const links = items.filter((i): i is PageLinkItem => i.kind === "pagelink");
    if (links.length === 0) return null;
    return (
      <div>
        {title && <SectionHeader title={title} subtitle={subtitle} />}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
          {links.map((l) => (
            <PageLinkTile key={l.path} link={l} />
          ))}
        </div>
      </div>
    );
  }

  // Default: horizontal grid of cards (handles HorizontalList,
  // ShortcutList, FeaturedItems, ItemList, HorizontalListWithContext, etc.)
  return (
    <div>
      {title && <SectionHeader title={title} subtitle={subtitle} />}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
        {items.map((it, idx) => (
          <PageItemCard key={`${it.kind}-${itemKey(it)}-${idx}`} item={it} onDownload={onDownload} />
        ))}
      </div>
    </div>
  );
}

function itemKey(i: PageItem): string {
  return "id" in i ? i.id : i.kind === "pagelink" ? i.path : "";
}

function SectionHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-4">
      <h2 className="text-xl font-bold tracking-tight">{title}</h2>
      {subtitle && (
        <p className="mt-0.5 text-sm text-muted-foreground">{subtitle}</p>
      )}
    </div>
  );
}

function PageItemCard({ item, onDownload }: { item: PageItem; onDownload: OnDownload }) {
  if (item.kind === "album" || item.kind === "artist" || item.kind === "playlist") {
    return <MediaCard item={item as Album | Artist | Playlist} onDownload={onDownload} />;
  }
  if (item.kind === "mix") {
    return <MixCard mix={item} />;
  }
  if (item.kind === "track") {
    const t = item as Track;
    return (
      <Link
        to={t.album ? `/album/${t.album.id}` : `/artist/${t.artists[0]?.id ?? ""}`}
        className="flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent"
      >
        <div className="aspect-square overflow-hidden rounded-md bg-secondary">
          {t.album?.cover ? (
            <img src={imageProxy(t.album.cover)} alt="" className="h-full w-full object-cover" />
          ) : (
            <Music className="m-auto h-10 w-10 text-muted-foreground" />
          )}
        </div>
        <div className="min-w-0">
          <div className="truncate font-semibold">{t.name}</div>
          <div className="truncate text-xs text-muted-foreground">
            {t.artists.map((a) => a.name).join(", ")}
          </div>
        </div>
      </Link>
    );
  }
  return null;
}

function MixCard({ mix }: { mix: MixItem }) {
  return (
    <Link
      to={`/mix/${encodeURIComponent(mix.id)}`}
      className="group flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent"
    >
      <div className="aspect-square overflow-hidden rounded-md bg-secondary">
        {mix.cover ? (
          <img
            src={imageProxy(mix.cover)}
            alt=""
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <Music className="m-auto h-10 w-10 text-muted-foreground" />
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{mix.name}</div>
        {mix.subtitle && (
          <div className="truncate text-xs text-muted-foreground">{mix.subtitle}</div>
        )}
      </div>
    </Link>
  );
}

function PageLinkTile({ link }: { link: PageLinkItem }) {
  // Deterministic color from the title so genre tiles have visual variety
  // without needing editorial artwork.
  const hue = hashHue(link.title);
  return (
    <Link
      to={`/browse/${encodeURIComponent(link.path)}`}
      className={cn(
        "relative flex aspect-[5/3] items-start overflow-hidden rounded-lg p-4 text-lg font-bold tracking-tight text-foreground transition-transform hover:scale-[1.02]",
      )}
      style={{ background: `linear-gradient(135deg, hsl(${hue}, 60%, 35%), hsl(${(hue + 40) % 360}, 70%, 20%))` }}
    >
      <span className="line-clamp-2">{link.title}</span>
    </Link>
  );
}

function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h) % 360;
}
