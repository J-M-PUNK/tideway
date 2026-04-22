import { useEffect, useMemo, useState } from "react";
import { Compass, Disc3, Download, Folder, Heart, LayoutGrid, Library as LibraryIcon, List, ListMusic, Loader2, Menu, Plus, User } from "lucide-react";
import { Link, Navigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { Album, Artist, Playlist, PlaylistFolder, Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { Grid } from "@/components/Grid";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { CreateFolderDialog } from "@/components/CreateFolderDialog";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { MediaCard } from "@/components/MediaCard";
import { MediaListRow } from "@/components/MediaListRow";
import {
  FormatFilter,
  type AudioFormat,
  hasAnyFormatTags,
  matchesFormat,
} from "@/components/FormatFilter";
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
type View = "grid" | "list";

const VIEW_KEY_PREFIX = "tideway:library-view:";

function loadView(section: Section): View {
  try {
    const v = localStorage.getItem(VIEW_KEY_PREFIX + section);
    return v === "list" ? "list" : "grid";
  } catch {
    return "grid";
  }
}

function saveView(section: Section, view: View): void {
  try {
    localStorage.setItem(VIEW_KEY_PREFIX + section, view);
  } catch {
    /* storage full or disabled */
  }
}

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
  const [folders, setFolders] = useState<PlaylistFolder[]>([]);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<Sort>("recent");
  const [view, setView] = useState<View>(() => loadView(type));
  // Format filter is ephemeral (per session), not persisted — it acts
  // like a sort, not a hard preference. Resets to "all" when the user
  // changes sections.
  const [format, setFormat] = useState<AudioFormat>("all");
  useEffect(() => {
    setFormat("all");
  }, [type]);

  // Rehydrate the view pref when switching sections — each section has
  // its own persisted preference (albums can be grid while playlists
  // is list, matching streaming-service UX).
  useEffect(() => {
    setView(loadView(type));
  }, [type]);

  const changeView = (v: View) => {
    setView(v);
    saveView(type, v);
  };

  useEffect(() => {
    setData(null);
    setFolders([]);
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
        if (!cancelled && type === "playlists") {
          // Folders live alongside playlists. Fetch them in parallel so
          // the main grid doesn't block on folder load.
          try {
            const f = await api.library.folders.list("root");
            if (!cancelled) setFolders(f);
          } catch {
            /* folders optional — silent */
          }
        }
      } catch (err) {
        if (!cancelled)
          setLoadError(err instanceof Error ? err : new Error(String(err)));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [type]);

  const refreshFolders = async () => {
    try {
      const f = await api.library.folders.list("root");
      setFolders(f);
    } catch {
      /* silent */
    }
  };

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = filter.trim().toLowerCase();
    let base = q
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
    // Audio-format filter. Only applies to sections whose items have
    // format tags (albums + tracks); artists / playlists ignore it
    // because the tags aren't meaningful at their level.
    if (format !== "all" && (type === "albums" || type === "tracks")) {
      base = base.filter((item) =>
        matchesFormat(item as { media_tags?: string[] }, format),
      );
    }
    if (sort === "alpha") {
      return [...base].sort((a, b) => ("name" in a ? a.name : "").localeCompare("name" in b ? b.name : ""));
    }
    return base; // "recent" — backend already returns newest-first
  }, [data, filter, sort, format, type]);

  // Hide the format filter entirely when the dataset has no tagged
  // items — otherwise it'd be a dead row of chips.
  const showFormatFilter =
    (type === "albums" || type === "tracks") &&
    data != null &&
    hasAnyFormatTags(data as Array<{ media_tags?: string[] }>);

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
          {type === "playlists" && (
            <>
              <CreateFolderDialog
                onCreated={refreshFolders}
                trigger={
                  <Button variant="outline" size="sm">
                    <Folder className="h-4 w-4" /> New folder
                  </Button>
                }
              />
              <CreatePlaylistDialog
                trigger={
                  <Button variant="outline" size="sm">
                    <Plus className="h-4 w-4" /> New playlist
                  </Button>
                }
              />
            </>
          )}
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            className="h-9 max-w-xs"
          />
          <SortMenu sort={sort} onSort={setSort} />
          {type !== "tracks" && <ViewToggle view={view} onChange={changeView} />}
        </div>
      </div>

      {showFormatFilter && (
        <div className="mb-4">
          <FormatFilter value={format} onChange={setFormat} />
        </div>
      )}

      {loadError && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-6 text-sm text-destructive">
          Couldn't load library: {loadError.message}
        </div>
      )}

      {!data && !loadError && (type === "tracks" ? <TrackListSkeleton /> : <GridSkeleton />)}

      {data && data.length === 0 && (
        <EmptyState
          icon={LibraryIcon}
          title={`No ${title.toLowerCase()} yet`}
          description={
            type === "playlists"
              ? "Create one below, or favorite a Tidal playlist to see it here."
              : emptyHint
          }
          action={
            type === "playlists" ? (
              <CreatePlaylistDialog
                trigger={
                  <Button variant="secondary" size="sm">
                    <Plus className="h-4 w-4" /> New playlist
                  </Button>
                }
              />
            ) : (
              <Button asChild variant="secondary" size="sm">
                <Link to="/explore">
                  <Compass className="h-4 w-4" /> Explore Tidal
                </Link>
              </Button>
            )
          }
        />
      )}

      {data && data.length > 0 && filtered.length === 0 && (
        <EmptyState icon={LibraryIcon} title="No matches" description={`Nothing matches "${filter}".`} />
      )}

      {data && filtered.length > 0 && type === "tracks" && (
        <TrackList tracks={filtered as Track[]} onDownload={onDownload} />
      )}

      {data && filtered.length > 0 && type !== "tracks" && (
        <>
          {type === "playlists" && folders.length > 0 && (
            // Folders render as their own row above the playlist grid —
            // matches the filesystem mental model (directories above
            // files) and keeps the folder affordance visible without
            // requiring a click-to-expand. Click a folder → drills
            // into its detail page.
            <div className="mb-8">
              <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Folders
              </h2>
              <Grid>
                {folders.map((f) => (
                  <FolderCard key={f.id} folder={f} />
                ))}
              </Grid>
            </div>
          )}
          {view === "grid" ? (
            <Grid>
              {(filtered as (Album | Artist | Playlist)[]).map((item) => (
                <MediaCard key={item.id} item={item} onDownload={onDownload} />
              ))}
            </Grid>
          ) : (
            <div className="flex flex-col gap-0.5">
              {(filtered as (Album | Artist | Playlist)[]).map((item) => (
                <MediaListRow
                  key={item.id}
                  item={item}
                  onDownload={onDownload}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function FolderCard({ folder }: { folder: PlaylistFolder }) {
  return (
    <Link
      to={`/library/folder/${encodeURIComponent(folder.id)}`}
      className="group flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent"
    >
      <div className="flex aspect-square items-center justify-center rounded-md bg-secondary">
        <Folder className="h-16 w-16 text-muted-foreground transition-colors group-hover:text-foreground" />
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{folder.name}</div>
        <div className="truncate text-xs text-muted-foreground">
          {folder.num_items} {folder.num_items === 1 ? "item" : "items"}
        </div>
      </div>
    </Link>
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

function ViewToggle({
  view,
  onChange,
}: {
  view: View;
  onChange: (v: View) => void;
}) {
  return (
    <div className="inline-flex rounded-md border border-border bg-secondary p-0.5">
      <button
        type="button"
        onClick={() => onChange("grid")}
        title="Grid view"
        aria-label="Grid view"
        aria-pressed={view === "grid"}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-sm transition-colors",
          view === "grid"
            ? "bg-background text-foreground"
            : "text-muted-foreground hover:text-foreground",
        )}
      >
        <LayoutGrid className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={() => onChange("list")}
        title="List view"
        aria-label="List view"
        aria-pressed={view === "list"}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-sm transition-colors",
          view === "list"
            ? "bg-background text-foreground"
            : "text-muted-foreground hover:text-foreground",
        )}
      >
        <Menu className="h-4 w-4" />
      </button>
    </div>
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
