import { useEffect, useMemo, useState } from "react";
import { Disc3, Download, Heart, Library as LibraryIcon, List, ListMusic, Loader2, User } from "lucide-react";
import { Navigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { Album, Artist, Playlist, Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { Grid } from "@/components/Grid";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { MediaCard } from "@/components/MediaCard";
import { TrackList } from "@/components/TrackList";
import { EmptyState } from "@/components/EmptyState";
import { GridSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

type Section = "albums" | "artists" | "playlists" | "tracks";
type Sort = "recent" | "alpha";

const META: Record<Section, { title: string; icon: typeof Disc3; emptyHint: string }> = {
  albums: {
    title: "Albums",
    icon: Disc3,
    emptyHint: "Albums you favorite in Tidal will appear here.",
  },
  artists: {
    title: "Artists",
    icon: User,
    emptyHint: "Artists you follow in Tidal will appear here.",
  },
  playlists: {
    title: "Playlists",
    icon: ListMusic,
    emptyHint: "Your own + favorited Tidal playlists will appear here.",
  },
  tracks: {
    title: "Liked Songs",
    icon: Heart,
    emptyHint: "Tracks you heart in Tidal will appear here.",
  },
};

type LibraryItem = Album | Artist | Playlist | Track;

export function Library({ onDownload }: { onDownload: OnDownload }) {
  const { section = "albums" } = useParams<{ section: string }>();
  // Guard against stale bookmarks like /library/typo — without this the
  // destructure of META[type] throws a TypeError that crashes the Shell
  // with no error boundary in its path.
  if (!(section in META)) {
    return <Navigate to="/library/albums" replace />;
  }
  const type = section as Section;
  const { title, icon: Icon, emptyHint } = META[type];

  const [data, setData] = useState<LibraryItem[] | null>(null);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<Sort>("recent");

  useEffect(() => {
    setData(null);
    setLoadError(null);
    setFilter("");
    let cancelled = false;
    (async () => {
      try {
        const items = await (type === "albums"
          ? api.library.albums()
          : type === "artists"
            ? api.library.artists()
            : type === "playlists"
              ? api.library.playlists()
              : api.library.tracks());
        if (!cancelled) setData(items);
      } catch (err) {
        if (!cancelled)
          setLoadError(err instanceof Error ? err : new Error(String(err)));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [type]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = filter.trim().toLowerCase();
    const base = q
      ? data.filter((item) =>
          [
            "name" in item ? item.name : "",
            "artists" in item && item.artists ? item.artists.map((a) => a.name).join(" ") : "",
            "creator" in item && item.creator ? item.creator : "",
          ]
            .join(" ")
            .toLowerCase()
            .includes(q),
        )
      : data;
    if (sort === "alpha") {
      return [...base].sort((a, b) => ("name" in a ? a.name : "").localeCompare("name" in b ? b.name : ""));
    }
    return base; // "recent" — backend already returns newest-first
  }, [data, filter, sort]);

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
          <Icon className="h-7 w-7" /> {title}
        </h1>
        <div className="flex items-center gap-2">
          {type === "tracks" && data && data.length > 0 && (
            <DownloadAllTracks tracks={data as Track[]} />
          )}
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            className="h-9 max-w-xs"
          />
          <SortMenu sort={sort} onSort={setSort} />
        </div>
      </div>

      {loadError && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-6 text-sm text-destructive">
          Couldn't load library: {loadError.message}
        </div>
      )}

      {!data && !loadError && (type === "tracks" ? <TrackListSkeleton /> : <GridSkeleton />)}

      {data && data.length === 0 && (
        <EmptyState icon={LibraryIcon} title={`No ${title.toLowerCase()} yet`} description={emptyHint} />
      )}

      {data && data.length > 0 && filtered.length === 0 && (
        <EmptyState icon={LibraryIcon} title="No matches" description={`Nothing matches "${filter}".`} />
      )}

      {data && filtered.length > 0 && type === "tracks" && (
        <TrackList tracks={filtered as Track[]} onDownload={onDownload} />
      )}

      {data && filtered.length > 0 && type !== "tracks" && (
        <Grid>
          {(filtered as (Album | Artist | Playlist)[]).map((item) => (
            <MediaCard key={item.id} item={item} onDownload={onDownload} />
          ))}
        </Grid>
      )}
    </div>
  );
}

function DownloadAllTracks({ tracks }: { tracks: Track[] }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (busy || tracks.length === 0) return;
    setBusy(true);
    try {
      const res = await api.downloads.enqueueBulk(
        tracks.map((t) => ({ kind: "track" as const, id: t.id })),
      );
      toast.show({
        kind: "success",
        title: `Queueing ${res.submitted} tracks`,
        description: "Running in the background.",
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't download all",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button size="sm" variant="outline" onClick={run} disabled={busy}>
      {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
      Download all
    </Button>
  );
}

function SortMenu({ sort, onSort }: { sort: Sort; onSort: (s: Sort) => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">
          <List className="h-4 w-4" />
          {sort === "alpha" ? "A–Z" : "Recent"}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Sort by</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onSort("recent")}>
          <span className={cn(sort === "recent" ? "text-primary" : "")}>Recently added</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onSort("alpha")}>
          <span className={cn(sort === "alpha" ? "text-primary" : "")}>Alphabetical</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
