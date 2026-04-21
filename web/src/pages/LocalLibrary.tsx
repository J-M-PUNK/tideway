import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Disc3, FolderOpen, HardDrive, Music, Play, User } from "lucide-react";
import { api } from "@/api/client";
import type { LocalFile, Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/EmptyState";
import { TrackListSkeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { formatDuration } from "@/lib/utils";
import { cn } from "@/lib/utils";

type GroupMode = "artist" | "folder";

/**
 * Browse the user's downloaded files directly off disk — complement to
 * /library/* which show what Tidal considers favorited. "By artist"
 * groups by the artist tag; "By folder" groups by the immediate parent
 * directory, which matches how the downloader lays out files when
 * album-folders is enabled.
 */
export function LocalLibrary({ onDownload: _onDownload }: { onDownload: OnDownload }) {
  const [data, setData] = useState<{ output_dir: string; files: LocalFile[] } | null>(null);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [filter, setFilter] = useState("");
  const [groupMode, setGroupMode] = useState<GroupMode>("artist");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    api.library
      .local()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err : new Error(String(err)));
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

  const groups = useMemo(() => {
    const m = new Map<string, LocalFile[]>();
    for (const f of filtered) {
      const key = groupMode === "artist" ? f.artist : folderOf(f);
      const list = m.get(key);
      if (list) list.push(f);
      else m.set(key, [f]);
    }
    return Array.from(m.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [filtered, groupMode]);

  const totalBytes = data?.files.reduce((n, f) => n + f.size_bytes, 0) ?? 0;

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

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
            <HardDrive className="h-7 w-7" /> On this device
          </h1>
          {data && (
            <p className="mt-1 text-sm text-muted-foreground">
              {data.files.length.toLocaleString()} file{data.files.length === 1 ? "" : "s"} ·{" "}
              {formatBytes(totalBytes)} · {data.output_dir}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            className="h-9 max-w-xs"
          />
          <GroupToggle value={groupMode} onChange={setGroupMode} />
        </div>
      </div>

      {!data && !loadError && <TrackListSkeleton />}

      {data && data.files.length === 0 && (
        <EmptyState
          icon={Music}
          title="No downloaded files yet"
          description="Tracks you download will show up here, grouped by artist or folder."
        />
      )}

      {data && data.files.length > 0 && filtered.length === 0 && (
        <EmptyState icon={Music} title="No matches" description={`Nothing matches "${filter}".`} />
      )}

      {groups.length > 0 && (
        <div className="flex flex-col gap-6">
          {groups.map(([key, files]) => {
            const isCollapsed = collapsed.has(key);
            return (
              <section key={key}>
                <button
                  onClick={() => toggleGroup(key)}
                  className="mb-2 flex w-full items-center gap-2 text-left"
                >
                  {isCollapsed ? (
                    <ChevronRight className="h-4 w-4 text-muted-foreground" />
                  ) : (
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  )}
                  {groupMode === "artist" ? (
                    <User className="h-4 w-4 text-muted-foreground" />
                  ) : (
                    <FolderOpen className="h-4 w-4 text-muted-foreground" />
                  )}
                  <span className="font-semibold">{key}</span>
                  <span className="text-xs text-muted-foreground">
                    {files.length} track{files.length === 1 ? "" : "s"}
                  </span>
                </button>
                {!isCollapsed && <LocalGroupList files={files} allFiles={filtered} />}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

function LocalGroupList({ files, allFiles }: { files: LocalFile[]; allFiles: LocalFile[] }) {
  return (
    <div className="overflow-hidden rounded-md border border-border/50">
      {files.map((f, i) => (
        <LocalRow key={f.path} file={f} rowIndex={i} files={files} allFiles={allFiles} />
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
    const queue = files
      .filter((f) => f.tidal_id)
      .map(toTrack);
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
        <div className="truncate text-xs text-muted-foreground">{file.artist}</div>
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

function GroupToggle({ value, onChange }: { value: GroupMode; onChange: (v: GroupMode) => void }) {
  const options: { value: GroupMode; label: string; icon: typeof User }[] = [
    { value: "artist", label: "By artist", icon: User },
    { value: "folder", label: "By folder", icon: FolderOpen },
  ];
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

function folderOf(f: LocalFile): string {
  // On Windows the relative path may use backslashes; normalize so the
  // group key matches regardless of how the backend happened to format
  // it. Last segment is the filename — drop it.
  const parts = f.relative_path.replace(/\\/g, "/").split("/");
  parts.pop();
  return parts.join("/") || "(root)";
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
