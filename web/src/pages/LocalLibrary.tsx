import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  ChevronDown,
  ChevronRight,
  Clock,
  Disc3,
  Film,
  FolderOpen,
  Music,
  Play,
  User,
} from "lucide-react";
import { api } from "@/api/client";
import type { LocalFile, LocalVideo, Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/EmptyState";
import { TrackListSkeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { formatDuration, imageProxy } from "@/lib/utils";
import { cn } from "@/lib/utils";

// Resolved Tidal track metadata cached by tidal_id. Multiple group
// sections asking for the same id share one in-flight request and
// the resolved result. Keyed by tidal_id (string). The cache lives
// for the page's lifetime — switching tabs / sort modes shouldn't
// re-resolve. Also dedupes work across re-renders.
const _trackResolutionCache = new Map<string, Track | null>();
const _trackResolutionInflight = new Map<string, Promise<Track | null>>();

function resolveTrack(tidalId: string): Promise<Track | null> {
  const cached = _trackResolutionCache.get(tidalId);
  if (cached !== undefined) return Promise.resolve(cached);
  const inflight = _trackResolutionInflight.get(tidalId);
  if (inflight) return inflight;
  const p = api
    .track(tidalId)
    .then((t) => {
      _trackResolutionCache.set(tidalId, t);
      return t;
    })
    .catch(() => {
      // Sign-out, network, rate limit. Cache the negative result
      // so we don't retry on every render — the user will get a
      // fresh resolution attempt only after a hard refresh.
      _trackResolutionCache.set(tidalId, null);
      return null;
    })
    .finally(() => {
      _trackResolutionInflight.delete(tidalId);
    });
  _trackResolutionInflight.set(tidalId, p);
  return p;
}

// Same idea for artist resolution. Used by the artists tab to fetch
// the picture URL when a per-track lookup hands us an artist id.
const _artistPictureCache = new Map<string, string | null>();
const _artistPictureInflight = new Map<string, Promise<string | null>>();

function resolveArtistPicture(artistId: string): Promise<string | null> {
  const cached = _artistPictureCache.get(artistId);
  if (cached !== undefined) return Promise.resolve(cached);
  const inflight = _artistPictureInflight.get(artistId);
  if (inflight) return inflight;
  const p = api
    .artist(artistId)
    .then((a) => {
      const pic = a.picture ?? null;
      _artistPictureCache.set(artistId, pic);
      return pic;
    })
    .catch(() => {
      _artistPictureCache.set(artistId, null);
      return null;
    })
    .finally(() => {
      _artistPictureInflight.delete(artistId);
    });
  _artistPictureInflight.set(artistId, p);
  return p;
}

/** How music on the local-library page is grouped:
 *  - "albums" (default): iTunes-style, one section per album. Best
 *    for users who think about their library in album terms.
 *  - "artists": one section per artist, tracks listed flat inside —
 *    useful for quickly finding "everything by X" regardless of album.
 *  - "recent": flat list sorted by download time (newest first) —
 *    answers "what did I just download?"
 */
type MusicSort = "albums" | "artists" | "recent";
/** Videos have no album concept, so just artist vs. recent. */
type VideoSort = "artists" | "recent";
type Tab = "music" | "videos";

/** Return the album-level "primary" artist for a track, used to keep
 *  guest-credited tracks grouped with the rest of their album. New
 *  downloads carry an explicit `album_artist` tag (FLAC `albumartist`
 *  / MP4 `aART`); older downloads predate that tag, so we fall back
 *  to the first comma-separated entry of the per-track `artist`
 *  string, since `_artist_names` joins with ", " at download time. */
function primaryArtist(f: LocalFile): string {
  const aa = f.album_artist?.trim();
  if (aa) return aa;
  const first = f.artist?.split(",")[0]?.trim();
  return first || "(Unknown)";
}

/**
 * Browse the user's downloaded files directly off disk — complement to
 * /library/* which show what Tidal considers favorited. Music can be
 * grouped by Albums (default), Artists, or Recent; Videos by Artists
 * or Recent. "Recent" is a flat list sorted by mtime so the user can
 * answer "what did I just download?" without scrolling.
 */
export function LocalLibrary({
  onDownload: _onDownload,
}: {
  onDownload: OnDownload;
}) {
  const [data, setData] = useState<{
    output_dir: string;
    videos_dir: string;
    files: LocalFile[];
    videos: LocalVideo[];
  } | null>(null);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [filter, setFilter] = useState("");
  const [musicSort, setMusicSort] = useState<MusicSort>("albums");
  const [videoSort, setVideoSort] = useState<VideoSort>("artists");
  const [tab, setTab] = useState<Tab>("music");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // Track which sort modes have been initialized for default-collapse.
  // The first time a sort/tab combination is rendered with at least one
  // group, we collapse all of them so the user starts from a tidy
  // overview rather than a 50-album wall of tracks. After that, the
  // user's manual expand/collapse state stands. Switching back to a
  // previously-initialized mode preserves whatever the user had open.
  const initializedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    api.library
      .local()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((err) => {
        if (!cancelled)
          setLoadError(err instanceof Error ? err : new Error(String(err)));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return data.files;
    return data.files.filter((f) =>
      `${f.title} ${f.artist} ${f.album}`.toLowerCase().includes(q),
    );
  }, [data, filter]);

  const filteredVideos = useMemo(() => {
    if (!data) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return data.videos;
    return data.videos.filter((v) =>
      `${v.title} ${v.artist}`.toLowerCase().includes(q),
    );
  }, [data, filter]);

  const musicGroups = useMemo(() => {
    if (musicSort === "recent") {
      // Recent is flat — one "group" holding everything in mtime
      // order. An empty string key signals the render path to hide
      // the header chrome and just show the rows.
      return [["", [...filtered].sort((a, b) => b.mtime - a.mtime)]] as [
        string,
        LocalFile[],
      ][];
    }
    const m = new Map<string, LocalFile[]>();
    for (const f of filtered) {
      // Albums view keys on "AlbumArtist · Album" so two albums with
      // the same name by different artists don't merge (Greatest
      // Hits edge case), and tracks with guest credits still group
      // under their album's primary artist instead of splitting off
      // (e.g. "Michael Jackson, Paul McCartney" on Thriller). Section
      // header still reads nicely because we split the key when
      // rendering.
      const primary = primaryArtist(f);
      const key =
        musicSort === "albums"
          ? `${primary} • ${f.album || "(Untitled album)"}`
          : primary;
      const list = m.get(key);
      if (list) list.push(f);
      else m.set(key, [f]);
    }
    const entries = Array.from(m.entries());
    if (musicSort === "albums") {
      // Sort each album's tracks by track number for album-view
      // sanity; artist view leaves the backend's sort intact
      // (artist → album → track_num), which already reads well.
      for (const [, list] of entries) {
        list.sort((a, b) => a.track_num - b.track_num);
      }
    }
    entries.sort(([a], [b]) => a.localeCompare(b));
    return entries;
  }, [filtered, musicSort]);

  const videoGroups = useMemo(() => {
    if (videoSort === "recent") {
      return [["", [...filteredVideos].sort((a, b) => b.mtime - a.mtime)]] as [
        string,
        LocalVideo[],
      ][];
    }
    const m = new Map<string, LocalVideo[]>();
    for (const v of filteredVideos) {
      const key = v.artist || "(Unknown artist)";
      const list = m.get(key);
      if (list) list.push(v);
      else m.set(key, [v]);
    }
    return Array.from(m.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [filteredVideos, videoSort]);

  const totalBytes = data?.files.reduce((n, f) => n + f.size_bytes, 0) ?? 0;
  const totalVideoBytes =
    data?.videos.reduce((n, v) => n + v.size_bytes, 0) ?? 0;

  // Default-collapse on first render of each sort mode. Album view
  // and Artist view both benefit (the user can scroll a clean list
  // and expand only what they want); Recent is flat with no groups
  // so it doesn't apply. Same idea for the videos tab. We track per-
  // mode initialization in a ref so toggling around the UI doesn't
  // override the user's actual collapse choices.
  useEffect(() => {
    if (!data) return;
    if (tab === "music") {
      if (musicSort !== "albums" && musicSort !== "artists") return;
      const seenKey = `music:${musicSort}`;
      if (initializedRef.current.has(seenKey)) return;
      if (musicGroups.length === 0) return;
      initializedRef.current.add(seenKey);
      setCollapsed((prev) => {
        const next = new Set(prev);
        for (const [key] of musicGroups) {
          if (key) next.add(key);
        }
        return next;
      });
    } else if (tab === "videos") {
      if (videoSort !== "artists") return;
      const seenKey = `videos:${videoSort}`;
      if (initializedRef.current.has(seenKey)) return;
      if (videoGroups.length === 0) return;
      initializedRef.current.add(seenKey);
      setCollapsed((prev) => {
        const next = new Set(prev);
        for (const [key] of videoGroups) {
          if (key) next.add(`video:${key}`);
        }
        return next;
      });
    }
  }, [data, tab, musicSort, videoSort, musicGroups, videoGroups]);

  const toggleGroup = (key: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  if (loadError)
    return (
      <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-6 text-sm text-destructive">
        Couldn't load local library: {loadError.message}
      </div>
    );

  const showingMusic = tab === "music";
  const countText = showingMusic
    ? data
      ? `${data.files.length.toLocaleString()} file${
          data.files.length === 1 ? "" : "s"
        } · ${formatBytes(totalBytes)} · ${data.output_dir}`
      : ""
    : data
      ? `${data.videos.length.toLocaleString()} video${
          data.videos.length === 1 ? "" : "s"
        } · ${formatBytes(totalVideoBytes)} · ${data.videos_dir}`
      : "";

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          {data && <p className="text-sm text-muted-foreground">{countText}</p>}
        </div>
        <div className="flex items-center gap-2">
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            className="h-9 max-w-xs"
          />
          {showingMusic ? (
            <SortToggle<MusicSort>
              value={musicSort}
              onChange={setMusicSort}
              options={[
                { value: "albums", label: "Albums", icon: Disc3 },
                { value: "artists", label: "Artists", icon: User },
                { value: "recent", label: "Recent", icon: Clock },
              ]}
            />
          ) : (
            <SortToggle<VideoSort>
              value={videoSort}
              onChange={setVideoSort}
              options={[
                { value: "artists", label: "Artists", icon: User },
                { value: "recent", label: "Recent", icon: Clock },
              ]}
            />
          )}
        </div>
      </div>

      <TabStrip
        value={tab}
        onChange={setTab}
        musicCount={data?.files.length ?? 0}
        videoCount={data?.videos.length ?? 0}
      />

      {!data && !loadError && <TrackListSkeleton />}

      {showingMusic ? (
        <>
          {data && data.files.length === 0 && (
            <EmptyState
              icon={Music}
              title="No downloaded tracks yet"
              description="Tracks you download will show up here. Switch between Albums, Artists, and Recent above."
            />
          )}
          {data && data.files.length > 0 && filtered.length === 0 && (
            <EmptyState
              icon={Music}
              title="No matches"
              description={`Nothing matches "${filter}".`}
            />
          )}
          {musicGroups.length > 0 && (
            <div className="flex flex-col gap-6">
              {musicGroups.map(([key, files]) => (
                <MusicGroupSection
                  key={key || "__flat__"}
                  groupKey={key}
                  files={files}
                  allFiles={filtered}
                  sort={musicSort}
                  collapsed={collapsed}
                  onToggle={toggleGroup}
                />
              ))}
            </div>
          )}
        </>
      ) : (
        <>
          {data && data.videos.length === 0 && (
            <EmptyState
              icon={Film}
              title="No downloaded videos yet"
              description="Music videos you download will show up here."
            />
          )}
          {data && data.videos.length > 0 && filteredVideos.length === 0 && (
            <EmptyState
              icon={Film}
              title="No matches"
              description={`Nothing matches "${filter}".`}
            />
          )}
          {videoGroups.length > 0 && (
            <div className="flex flex-col gap-6">
              {videoGroups.map(([key, videos]) => (
                <VideoGroupSection
                  key={key || "__flat__"}
                  groupKey={key}
                  videos={videos}
                  collapsed={collapsed}
                  onToggle={toggleGroup}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function TabStrip({
  value,
  onChange,
  musicCount,
  videoCount,
}: {
  value: Tab;
  onChange: (t: Tab) => void;
  musicCount: number;
  videoCount: number;
}) {
  const tabs: { id: Tab; label: string; icon: typeof Music; count: number }[] =
    [
      { id: "music", label: "Music", icon: Music, count: musicCount },
      { id: "videos", label: "Videos", icon: Film, count: videoCount },
    ];
  return (
    <div className="mb-6 flex border-b border-border">
      {tabs.map((t) => {
        const Icon = t.icon;
        const active = t.id === value;
        return (
          <button
            key={t.id}
            onClick={() => onChange(t.id)}
            className={cn(
              "-mb-px flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-medium transition-colors",
              active
                ? "border-foreground text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-4 w-4" />
            {t.label}
            <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] font-bold">
              {t.count}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function LocalVideoGroupList({ videos }: { videos: LocalVideo[] }) {
  return (
    <div className="overflow-hidden rounded-md border border-border/50">
      {videos.map((v, i) => (
        <LocalVideoRow key={v.path} video={v} rowIndex={i} />
      ))}
    </div>
  );
}

function LocalVideoRow({
  video,
  rowIndex,
}: {
  video: LocalVideo;
  rowIndex: number;
}) {
  const toast = useToast();
  const reveal = async () => {
    try {
      await api.downloads.reveal(video.path);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't show file",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };
  return (
    <div
      className={cn(
        "group grid select-none grid-cols-[40px_5fr_2fr_auto] items-center gap-3 px-3 py-2 text-sm",
        rowIndex !== 0 && "border-t border-border/50",
      )}
    >
      <div className="flex justify-center text-muted-foreground">
        <Film className="h-4 w-4" />
      </div>
      <div className="min-w-0">
        <div className="truncate font-medium">{video.title}</div>
        <div className="truncate text-xs text-muted-foreground">
          {video.artist}
        </div>
      </div>
      <div className="truncate text-xs uppercase tracking-wider text-muted-foreground">
        {video.ext.replace(".", "")} · {formatBytes(video.size_bytes)}
      </div>
      <div className="flex justify-end">
        <Button
          size="sm"
          variant="ghost"
          onClick={reveal}
          title="Show in file explorer"
        >
          <FolderOpen className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function LocalGroupList({
  files,
  allFiles,
}: {
  files: LocalFile[];
  allFiles: LocalFile[];
}) {
  return (
    <div className="overflow-hidden rounded-md border border-border/50">
      {files.map((f, i) => (
        <LocalRow
          key={f.path}
          file={f}
          rowIndex={i}
          files={files}
          allFiles={allFiles}
        />
      ))}
    </div>
  );
}

function LocalRow({
  file,
  rowIndex,
  files,
  allFiles,
}: {
  file: LocalFile;
  rowIndex: number;
  files: LocalFile[];
  allFiles: LocalFile[];
}) {
  const actions = usePlayerActions();
  const toast = useToast();

  const play = () => {
    if (!file.tidal_id) {
      toast.show({
        kind: "info",
        title: "Can't play this file",
        description:
          "This file wasn't downloaded by the app, so it has no Tidal ID. Re-download it to play from here.",
      });
      return;
    }
    // Build synthetic Track objects for the whole group so the player's
    // queue works (prev/next within this artist or folder). Tracks without
    // tidal_id are dropped — the player can't stream them.
    const queue = files.filter((f) => f.tidal_id).map(toTrack);
    const start = queue.find((t) => t.id === file.tidal_id) ?? queue[0];
    if (start) actions.play(start, queue);
  };

  const reveal = async () => {
    try {
      await api.downloads.reveal(file.path);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't show file",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  void allFiles; // reserved for future "Play all matching" action

  return (
    <div
      onDoubleClick={file.tidal_id ? play : undefined}
      className={cn(
        "group grid select-none grid-cols-[40px_4fr_3fr_2fr_80px_auto] items-center gap-3 px-3 py-2 text-sm",
        file.tidal_id && "cursor-default",
        rowIndex !== 0 && "border-t border-border/50",
      )}
    >
      <div className="flex justify-center">
        <button
          onClick={play}
          className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100"
          title={file.tidal_id ? "Play" : "No Tidal ID — cannot play"}
        >
          <Play className="h-3.5 w-3.5" fill="currentColor" />
        </button>
      </div>
      <div className="min-w-0">
        <div className="truncate font-medium">{file.title}</div>
        <div className="truncate text-xs text-muted-foreground">
          {file.artist}
        </div>
      </div>
      <div className="flex min-w-0 items-center gap-1.5 text-muted-foreground">
        <Disc3 className="h-3.5 w-3.5 flex-shrink-0" />
        <span className="truncate text-xs">{file.album}</span>
      </div>
      <div className="truncate text-xs uppercase tracking-wider text-muted-foreground">
        {file.ext.replace(".", "")} · {formatBytes(file.size_bytes)}
      </div>
      <div className="text-right tabular-nums text-xs text-muted-foreground">
        {file.duration > 0 ? formatDuration(file.duration) : ""}
      </div>
      <div className="flex justify-end">
        <Button
          size="sm"
          variant="ghost"
          onClick={reveal}
          title="Show in file explorer"
        >
          <FolderOpen className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function SortToggle<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string; icon: typeof User }[];
}) {
  return (
    <div className="inline-flex rounded-md border border-border bg-secondary p-0.5">
      {options.map((opt) => {
        const Icon = opt.icon;
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            onClick={() => onChange(opt.value)}
            className={cn(
              "flex items-center gap-1.5 rounded px-3 py-1 text-xs font-semibold transition-colors",
              active
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * One music group section — an album or artist header + its tracks.
 * `groupKey === ""` signals a flat list (Recent sort) which hides
 * the collapse chrome and just renders rows directly. Albums view
 * keys as "Artist • Album" so we can split them back into a
 * two-line header ("Album" big, "Artist" small underneath).
 */
function MusicGroupSection({
  groupKey,
  files,
  allFiles,
  sort,
  collapsed,
  onToggle,
}: {
  groupKey: string;
  files: LocalFile[];
  allFiles: LocalFile[];
  sort: MusicSort;
  collapsed: Set<string>;
  onToggle: (key: string) => void;
}) {
  // Resolve the group's first track that carries a Tidal id, so we
  // can fetch the album cover (albums view) or the artist picture
  // (artists view). Tracks without a tidal_id (legacy / sideloaded
  // files) are skipped here — the corresponding header just falls
  // back to the icon placeholder.
  const firstTidalId = useMemo(
    () => files.find((f) => f.tidal_id)?.tidal_id ?? null,
    [files],
  );
  const [resolved, setResolved] = useState<Track | null>(null);
  useEffect(() => {
    if (!firstTidalId) return;
    let cancelled = false;
    resolveTrack(firstTidalId).then((t) => {
      if (!cancelled) setResolved(t);
    });
    return () => {
      cancelled = true;
    };
  }, [firstTidalId]);

  // For the Artists tab we also want the artist's portrait — the
  // album.cover we get from the track resolution is the wrong art.
  // Chain a second fetch after the track resolves so we know which
  // artist id to ask for.
  const [artistPicture, setArtistPicture] = useState<string | null>(null);
  useEffect(() => {
    if (sort !== "artists") return;
    const artistId = resolved?.artists[0]?.id;
    if (!artistId) return;
    let cancelled = false;
    resolveArtistPicture(artistId).then((pic) => {
      if (!cancelled) setArtistPicture(pic);
    });
    return () => {
      cancelled = true;
    };
  }, [sort, resolved]);

  if (groupKey === "") {
    // Recent mode — flat list, no header, no collapse.
    return (
      <section>
        <LocalGroupList files={files} allFiles={allFiles} />
      </section>
    );
  }
  const isCollapsed = collapsed.has(groupKey);

  let heading = groupKey;
  let subheading: string | null = null;
  if (sort === "albums") {
    const idx = groupKey.indexOf(" • ");
    if (idx > 0) {
      subheading = groupKey.slice(0, idx);
      heading = groupKey.slice(idx + 3);
    }
  }

  // Clickable destinations come from the resolved Track object —
  // null (sign-out, sideload, network) keeps the heading as plain
  // text. Albums view links the title to /album, the artist
  // subheading to /artist. Artists view links the title to /artist.
  const albumHref =
    sort === "albums" && resolved?.album?.id
      ? `/album/${resolved.album.id}`
      : null;
  const artistHref = resolved?.artists[0]?.id
    ? `/artist/${resolved.artists[0].id}`
    : null;

  // Avatar choice: Artists tab → artist portrait if we have one,
  // otherwise the User glyph. Albums tab → album cover if we have
  // one, otherwise the Disc glyph.
  const avatar =
    sort === "artists" ? (
      artistPicture ? (
        <img
          src={imageProxy(artistPicture)}
          alt=""
          className="h-12 w-12 shrink-0 rounded-full object-cover"
          loading="lazy"
        />
      ) : (
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-secondary text-muted-foreground">
          <User className="h-6 w-6" />
        </div>
      )
    ) : resolved?.album?.cover ? (
      <img
        src={imageProxy(resolved.album.cover)}
        alt=""
        className="h-12 w-12 shrink-0 rounded-md object-cover"
        loading="lazy"
      />
    ) : (
      <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground">
        <Disc3 className="h-6 w-6" />
      </div>
    );

  return (
    <section>
      <div className="mb-2 flex items-center gap-3">
        <button
          onClick={() => onToggle(groupKey)}
          className="flex h-6 w-6 shrink-0 items-center justify-center text-muted-foreground hover:text-foreground"
          aria-label={isCollapsed ? "Expand" : "Collapse"}
        >
          {isCollapsed ? (
            <ChevronRight className="h-4 w-4" />
          ) : (
            <ChevronDown className="h-4 w-4" />
          )}
        </button>
        {avatar}
        <div className="min-w-0 flex-1">
          {sort === "albums" && albumHref ? (
            <Link
              to={albumHref}
              className="truncate font-semibold hover:underline"
            >
              {heading}
            </Link>
          ) : sort === "artists" && artistHref ? (
            <Link
              to={artistHref}
              className="truncate font-semibold hover:underline"
            >
              {heading}
            </Link>
          ) : (
            <button
              type="button"
              onClick={() => onToggle(groupKey)}
              className="truncate font-semibold text-left"
            >
              {heading}
            </button>
          )}
          {subheading &&
            (artistHref ? (
              <Link
                to={artistHref}
                className="block truncate text-xs text-muted-foreground hover:underline"
              >
                {subheading}
              </Link>
            ) : (
              <div className="truncate text-xs text-muted-foreground">
                {subheading}
              </div>
            ))}
        </div>
        <span className="shrink-0 text-xs text-muted-foreground">
          {files.length} track{files.length === 1 ? "" : "s"}
        </span>
      </div>
      {!isCollapsed && <LocalGroupList files={files} allFiles={allFiles} />}
    </section>
  );
}

function VideoGroupSection({
  groupKey,
  videos,
  collapsed,
  onToggle,
}: {
  groupKey: string;
  videos: LocalVideo[];
  collapsed: Set<string>;
  onToggle: (key: string) => void;
}) {
  if (groupKey === "") {
    return (
      <section>
        <LocalVideoGroupList videos={videos} />
      </section>
    );
  }
  const cacheKey = `video:${groupKey}`;
  const isCollapsed = collapsed.has(cacheKey);
  return (
    <section>
      <button
        onClick={() => onToggle(cacheKey)}
        className="mb-2 flex w-full items-center gap-2 text-left"
      >
        {isCollapsed ? (
          <ChevronRight className="h-4 w-4 text-muted-foreground" />
        ) : (
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        )}
        <User className="h-4 w-4 text-muted-foreground" />
        <span className="font-semibold">{groupKey}</span>
        <span className="text-xs text-muted-foreground">
          {videos.length} video{videos.length === 1 ? "" : "s"}
        </span>
      </button>
      {!isCollapsed && <LocalVideoGroupList videos={videos} />}
    </section>
  );
}

function toTrack(f: LocalFile): Track {
  return {
    kind: "track",
    id: f.tidal_id ?? f.path,
    name: f.title,
    duration: f.duration,
    track_num: f.track_num,
    explicit: false,
    artists: [{ id: "", name: f.artist }],
    album: { id: "", name: f.album, cover: null },
  };
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
