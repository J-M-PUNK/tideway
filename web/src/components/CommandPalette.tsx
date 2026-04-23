import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Compass,
  Disc3,
  Download,
  Heart,
  Library,
  ListMusic,
  Loader2,
  Music,
  Plus,
  Search as SearchIcon,
  Settings,
  User,
} from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist, Playlist, SearchResponse, Track } from "@/api/types";
import { imageProxy, cn } from "@/lib/utils";

type Action = {
  kind: "action";
  id: string;
  title: string;
  hint: string;
  icon: React.ComponentType<{ className?: string }>;
  run: () => void;
};

type ContentResult =
  | { kind: "track"; item: Track }
  | { kind: "album"; item: Album }
  | { kind: "artist"; item: Artist }
  | { kind: "playlist"; item: Playlist };

type Row = Action | ContentResult;

export function CommandPalette({
  open,
  onClose,
  onOpenCreatePlaylist,
}: {
  open: boolean;
  onClose: () => void;
  onOpenCreatePlaylist: () => void;
}) {
  const [query, setQuery] = useState("");
  const [tidalResults, setTidalResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const debounceRef = useRef<number | null>(null);

  // Reset + autofocus each time we open.
  useEffect(() => {
    if (open) {
      setQuery("");
      setTidalResults(null);
      setSelected(0);
      // Wait a frame so the autofocus lands after the dialog mounts.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // Debounced Tidal search. Actions + local hits always render instantly.
  useEffect(() => {
    if (!open) return;
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    const q = query.trim();
    if (q.length < 2) {
      setTidalResults(null);
      setLoading(false);
      return;
    }
    // `cancelled` stops an older in-flight search from overwriting newer
    // results. Without it, "foo" can resolve AFTER "foobar" and clobber
    // the correct list. The debounce only deduplicates the SEND side.
    let cancelled = false;
    setLoading(true);
    debounceRef.current = window.setTimeout(() => {
      api
        .search(q, 6)
        .then((r) => {
          if (cancelled) return;
          setTidalResults(r);
        })
        .catch(() => {
          if (cancelled) return;
          setTidalResults(null);
        })
        .finally(() => {
          if (cancelled) return;
          setLoading(false);
        });
    }, 220);
    return () => {
      cancelled = true;
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, [query, open]);

  const actions = useMemo<Action[]>(
    () => [
      {
        kind: "action",
        id: "go-home",
        title: "Home",
        hint: "Go to home",
        icon: Music,
        run: () => navigate("/"),
      },
      {
        kind: "action",
        id: "go-explore",
        title: "Explore",
        hint: "Browse genres and moods",
        icon: Compass,
        run: () => navigate("/explore"),
      },
      {
        kind: "action",
        id: "go-library-albums",
        title: "Albums",
        hint: "Your library",
        icon: Disc3,
        run: () => navigate("/library/albums"),
      },
      {
        kind: "action",
        id: "go-library-artists",
        title: "Artists",
        hint: "Your library",
        icon: User,
        run: () => navigate("/library/artists"),
      },
      {
        kind: "action",
        id: "go-library-playlists",
        title: "Playlists",
        hint: "Your library",
        icon: ListMusic,
        run: () => navigate("/library/playlists"),
      },
      {
        kind: "action",
        id: "go-library-tracks",
        title: "Liked Songs",
        hint: "Your library",
        icon: Heart,
        run: () => navigate("/library/tracks"),
      },
      {
        kind: "action",
        id: "go-downloads",
        title: "Downloads",
        hint: "Manage downloads",
        icon: Download,
        run: () => navigate("/downloads"),
      },
      {
        kind: "action",
        id: "go-settings",
        title: "Settings",
        hint: "App settings",
        icon: Settings,
        run: () => navigate("/settings"),
      },
      {
        kind: "action",
        id: "new-playlist",
        title: "Create playlist",
        hint: "New playlist…",
        icon: Plus,
        run: () => {
          onClose();
          onOpenCreatePlaylist();
        },
      },
    ],
    [navigate, onClose, onOpenCreatePlaylist],
  );

  const rows = useMemo<Row[]>(() => {
    const q = query.trim().toLowerCase();
    const filteredActions = q
      ? actions.filter((a) => a.title.toLowerCase().includes(q) || a.hint.toLowerCase().includes(q))
      : actions;
    const content: Row[] = [];
    if (tidalResults) {
      for (const t of tidalResults.tracks.slice(0, 4))
        content.push({ kind: "track", item: t });
      for (const a of tidalResults.albums.slice(0, 3))
        content.push({ kind: "album", item: a });
      for (const a of tidalResults.artists.slice(0, 3))
        content.push({ kind: "artist", item: a });
      for (const p of tidalResults.playlists.slice(0, 2))
        content.push({ kind: "playlist", item: p });
    }
    return [...filteredActions, ...content];
  }, [actions, tidalResults, query]);

  const chooseRow = (row: Row) => {
    onClose();
    if (row.kind === "action") {
      row.run();
      return;
    }
    const { kind, item } = row;
    if (kind === "track" && item.album) navigate(`/album/${item.album.id}`);
    else if (kind === "album") navigate(`/album/${item.id}`);
    else if (kind === "artist") navigate(`/artist/${item.id}`);
    else if (kind === "playlist") navigate(`/playlist/${item.id}`);
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelected((s) => Math.min(s + 1, Math.max(rows.length - 1, 0)));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelected((s) => Math.max(s - 1, 0));
      } else if (e.key === "Enter") {
        const row = rows[selected];
        if (row) {
          e.preventDefault();
          chooseRow(row);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, rows, selected, onClose]);

  useEffect(() => {
    setSelected(0);
  }, [rows.length]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[80] flex items-start justify-center bg-black/60 backdrop-blur-sm pt-[15vh]"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Type a command, song, artist, or album…"
            className="w-full bg-transparent py-4 pl-12 pr-12 text-base outline-none"
          />
          {loading && (
            <Loader2 className="absolute right-4 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-muted-foreground" />
          )}
        </div>
        <div className="max-h-[60vh] overflow-y-auto border-t border-border scrollbar-thin">
          {rows.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-muted-foreground">
              {query ? "No matches." : "Start typing…"}
            </div>
          ) : (
            rows.map((row, i) => (
              <PaletteRow
                key={rowKey(row)}
                row={row}
                active={i === selected}
                onMouseMove={() => setSelected(i)}
                onClick={() => chooseRow(row)}
              />
            ))
          )}
        </div>
        <div className="flex items-center justify-between border-t border-border bg-secondary/40 px-4 py-2 text-[11px] text-muted-foreground">
          <span>↑↓ navigate · ↵ select · Esc close</span>
          <span className="flex items-center gap-1">
            <Library className="h-3 w-3" /> Tidal
          </span>
        </div>
      </div>
    </div>
  );
}

function rowKey(row: Row): string {
  if (row.kind === "action") return `action:${row.id}`;
  return `${row.kind}:${row.item.id}`;
}

function PaletteRow({
  row,
  active,
  onMouseMove,
  onClick,
}: {
  row: Row;
  active: boolean;
  onMouseMove: () => void;
  onClick: () => void;
}) {
  let title = "";
  let subtitle = "";
  let cover: string | null = null;
  let round = false;
  let Icon: React.ComponentType<{ className?: string }> | null = null;

  if (row.kind === "action") {
    title = row.title;
    subtitle = row.hint;
    Icon = row.icon;
  } else if (row.kind === "track") {
    title = row.item.name;
    subtitle = `Track · ${row.item.artists.map((a) => a.name).join(", ")}`;
    cover = row.item.album?.cover ?? null;
  } else if (row.kind === "album") {
    title = row.item.name;
    subtitle = `Album · ${row.item.artists.map((a) => a.name).join(", ")}`;
    cover = row.item.cover;
  } else if (row.kind === "artist") {
    title = row.item.name;
    subtitle = "Artist";
    cover = row.item.picture;
    round = true;
  } else if (row.kind === "playlist") {
    title = row.item.name;
    subtitle = `Playlist${row.item.creator ? ` · ${row.item.creator}` : ""}`;
    cover = row.item.cover;
  }

  return (
    <button
      onMouseMove={onMouseMove}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors",
        active ? "bg-accent" : "hover:bg-accent/60",
      )}
    >
      {Icon ? (
        <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded bg-secondary text-muted-foreground">
          <Icon className="h-4 w-4" />
        </div>
      ) : (
        <div
          className={cn(
            "h-9 w-9 flex-shrink-0 overflow-hidden bg-secondary",
            round ? "rounded-full" : "rounded",
          )}
        >
          {cover ? (
            <img
              src={imageProxy(cover)}
              alt=""
              className="h-full w-full object-cover"
              loading="lazy"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Music className="h-4 w-4" />
            </div>
          )}
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">{title}</div>
        <div className="truncate text-xs text-muted-foreground">{subtitle}</div>
      </div>
    </button>
  );
}
