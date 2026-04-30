import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Loader2, Music } from "lucide-react";
import { api } from "@/api/client";
import type {
  Album,
  Artist,
  Playlist,
  SearchResponse,
  Track,
} from "@/api/types";
import { cn, imageProxy } from "@/lib/utils";

/**
 * Inline typeahead dropdown that hangs under the NavBar's search
 * input. Reuses /api/search at a smaller limit and renders a compact
 * preview: top hit + a couple of rows from each kind, plus a
 * "Show all results" footer that lands on the full Search page.
 *
 * Click target depends on row kind: track → album page (no per-track
 * page exists), album / playlist → detail page, artist → profile.
 *
 * The component is uncontrolled outside of `query` and `open`. The
 * parent owns input focus, the query string, and the open/close
 * lifecycle; this just renders results and reports row activations.
 */

type ContentRow =
  | { kind: "track"; item: Track }
  | { kind: "album"; item: Album }
  | { kind: "artist"; item: Artist }
  | { kind: "playlist"; item: Playlist };

type Row = ContentRow | { kind: "see-all" };

export function SearchSuggestions({
  query,
  open,
  onActivate,
  onCloseRequested,
}: {
  query: string;
  open: boolean;
  /** Fired when the user picks a row (mouse or keyboard). The parent
   *  is expected to close the dropdown. */
  onActivate: () => void;
  /** Fired on Escape so the parent can clear focus / close. */
  onCloseRequested: () => void;
}) {
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(0);
  const navigate = useNavigate();
  const debounceRef = useRef<number | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Debounced fetch. <2 chars → don't bother; the server will return
  // mostly junk and the network round-trip is wasteful for one-letter
  // queries. Matches the CommandPalette's threshold so the two
  // surfaces feel the same.
  useEffect(() => {
    if (!open) return;
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    const q = query.trim();
    if (q.length < 2) {
      setResults(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    debounceRef.current = window.setTimeout(() => {
      api
        .search(q, 6)
        .then((r) => {
          if (cancelled) return;
          setResults(r);
        })
        .catch(() => {
          if (cancelled) return;
          setResults(null);
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

  // Build the row list. The top hit (when present) leads, then up to
  // 2 of each remaining kind. Keep the dropdown short — the full
  // Search page is one Enter / click away.
  const rows = useMemo<Row[]>(() => {
    if (!results) return [];
    const out: Row[] = [];
    const topHitId = results.top_hit
      ? `${results.top_hit.kind}:${results.top_hit.id}`
      : null;
    const seen = new Set<string>();
    if (results.top_hit) {
      const t = results.top_hit;
      out.push({ kind: t.kind, item: t } as ContentRow);
      seen.add(`${t.kind}:${t.id}`);
    }
    const take = (
      kind: ContentRow["kind"],
      items: Array<Track | Album | Artist | Playlist>,
      n: number,
    ) => {
      let added = 0;
      for (const it of items) {
        if (added >= n) break;
        const key = `${kind}:${it.id}`;
        if (seen.has(key) || key === topHitId) continue;
        seen.add(key);
        out.push({ kind, item: it } as ContentRow);
        added += 1;
      }
    };
    take("track", results.tracks, 3);
    take("artist", results.artists, 2);
    take("album", results.albums, 2);
    take("playlist", results.playlists, 1);
    if (out.length > 0) out.push({ kind: "see-all" });
    return out;
  }, [results]);

  // Reset selection whenever the row set changes so we don't point at
  // a stale index after a query refresh.
  useEffect(() => {
    setSelected(0);
  }, [rows.length]);

  const activate = (row: Row) => {
    onActivate();
    if (row.kind === "see-all") {
      navigate(`/search?q=${encodeURIComponent(query.trim())}`);
      return;
    }
    const { kind, item } = row;
    if (kind === "track" && item.album) navigate(`/album/${item.album.id}`);
    else if (kind === "album") navigate(`/album/${item.id}`);
    else if (kind === "artist") navigate(`/artist/${item.id}`);
    else if (kind === "playlist") navigate(`/playlist/${item.id}`);
  };

  // Keyboard handling lives at the document level so the user can
  // type / arrow-key without the input losing focus. Refs hold the
  // latest activate / onCloseRequested so the listener doesn't have
  // to re-attach on every render — handy when the parent recreates
  // the close callback inline (NavBar does).
  const activateRef = useRef(activate);
  const closeRef = useRef(onCloseRequested);
  activateRef.current = activate;
  closeRef.current = onCloseRequested;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeRef.current();
      } else if (e.key === "ArrowDown") {
        if (rows.length === 0) return;
        e.preventDefault();
        setSelected((s) => Math.min(s + 1, rows.length - 1));
      } else if (e.key === "ArrowUp") {
        if (rows.length === 0) return;
        e.preventDefault();
        setSelected((s) => Math.max(s - 1, 0));
      } else if (e.key === "Enter") {
        const row = rows[selected];
        if (row) {
          e.preventDefault();
          activateRef.current(row);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, rows, selected]);

  // Scroll the active row into view when arrowing past the visible
  // area. Mouse hover doesn't need this since the cursor itself is
  // what changed selection.
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(
      `[data-row-index="${selected}"]`,
    );
    el?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  if (!open) return null;

  const showEmpty = !loading && query.trim().length >= 2 && rows.length === 0;
  const tooShort = query.trim().length > 0 && query.trim().length < 2;

  return (
    <div
      className="absolute left-0 right-0 top-full z-20 mt-1 overflow-hidden rounded-md border border-border bg-popover text-popover-foreground shadow-lg"
      // Clicking inside the panel must not blur the input (which
      // would close the dropdown via the parent's blur handler before
      // the row's onClick fires).
      onMouseDown={(e) => e.preventDefault()}
    >
      <div
        ref={listRef}
        className="max-h-[60vh] overflow-y-auto scrollbar-thin"
      >
        {loading && rows.length === 0 && (
          <div className="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Searching…
          </div>
        )}
        {showEmpty && (
          <div className="px-4 py-3 text-sm text-muted-foreground">
            No matches.
          </div>
        )}
        {tooShort && (
          <div className="px-4 py-3 text-sm text-muted-foreground">
            Keep typing…
          </div>
        )}
        {rows.map((row, i) => (
          <SuggestionRow
            key={rowKey(row)}
            row={row}
            active={i === selected}
            index={i}
            onMouseMove={() => setSelected(i)}
            onClick={() => activate(row)}
          />
        ))}
      </div>
    </div>
  );
}

function rowKey(row: Row): string {
  if (row.kind === "see-all") return "see-all";
  return `${row.kind}:${row.item.id}`;
}

function SuggestionRow({
  row,
  active,
  index,
  onMouseMove,
  onClick,
}: {
  row: Row;
  active: boolean;
  index: number;
  onMouseMove: () => void;
  onClick: () => void;
}) {
  if (row.kind === "see-all") {
    return (
      <button
        data-row-index={index}
        type="button"
        onMouseMove={onMouseMove}
        onClick={onClick}
        className={cn(
          "flex w-full items-center justify-between border-t border-border px-4 py-2.5 text-left text-sm font-semibold transition-colors",
          active ? "bg-accent" : "hover:bg-accent/60",
        )}
      >
        <span>Show all results</span>
        <ArrowRight className="h-4 w-4" />
      </button>
    );
  }

  let title = "";
  let subtitle = "";
  let cover: string | null = null;
  let round = false;

  if (row.kind === "track") {
    title = row.item.name;
    subtitle = `Song · ${row.item.artists.map((a) => a.name).join(", ")}`;
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
      data-row-index={index}
      type="button"
      onMouseMove={onMouseMove}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors",
        active ? "bg-accent" : "hover:bg-accent/60",
      )}
    >
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
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">{title}</div>
        <div className="truncate text-xs text-muted-foreground">{subtitle}</div>
      </div>
    </button>
  );
}
