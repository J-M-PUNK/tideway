import { Link } from "react-router-dom";
import {
  Check,
  Clock,
  Loader2,
  MoreHorizontal,
  Music,
  Pause,
  Play,
} from "lucide-react";
import type { Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { formatDuration, imageProxy } from "@/lib/utils";
import { DownloadButton } from "@/components/DownloadButton";
import { EmptyState } from "@/components/EmptyState";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { useDownloadedIds, useIsDownloaded } from "@/hooks/useDownloadedSet";
import { useLastfmTrackPlaycount } from "@/hooks/useLastfmPlaycount";
import { useTrackSelection } from "@/hooks/useTrackSelection";
import { useUiPreferences } from "@/hooks/useUiPreferences";
import { HeartButton } from "@/components/HeartButton";
import { CreditsDialog } from "@/components/CreditsDialog";
import { cn } from "@/lib/utils";
import {
  CONTEXT_MENU_PARTS,
  DROPDOWN_MENU_PARTS,
  TrackMenuItems,
} from "@/components/TrackMenu";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  closestCenter,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

interface Props {
  tracks: Track[];
  onDownload: OnDownload;
  showAlbum?: boolean;
  numbered?: boolean;
  /**
   * When set, each row renders a trash icon that calls this handler with
   * the track's row index. Used on owned-playlist detail pages.
   */
  onRemove?: (index: number) => void;
  /**
   * When set, rows become drag-sortable. The handler receives the new
   * ordering — both the moved track's media id AND the new position — so
   * callers can send a minimal mutation to the server and reconcile
   * optimistically.
   */
  onReorder?: (mediaId: string, fromIndex: number, toIndex: number) => void;
  /**
   * When true, each row fetches its Last.fm global playcount and shows
   * it in its own column (right-aligned, before duration). Off by
   * default — only worth enabling on album + artist-popular views
   * where the extra per-row request is meaningful context. A 50-track
   * album will fire 50 calls, throttled server-side to 4 concurrent.
   */
  showPlaycount?: boolean;
}

export function TrackList({
  tracks,
  onDownload,
  showAlbum = true,
  numbered = true,
  onRemove,
  onReorder,
  showPlaycount = false,
}: Props) {
  const { offlineOnly } = useUiPreferences();
  // Optional offline-only filter — hides tracks Tidal knows about but that
  // aren't on disk yet. Applied at the list level so row indexes still line
  // up with the visible set for drag-reorder / selection logic.
  const downloadedIds = useDownloadedIds();
  const visibleTracks = offlineOnly
    ? tracks.filter((t) => downloadedIds.has(t.id))
    : tracks;
  if (visibleTracks.length === 0) {
    return (
      <EmptyState
        icon={Music}
        title={offlineOnly ? "Nothing downloaded yet" : "No tracks"}
        description={
          offlineOnly
            ? "Turn off offline-only in Settings to see streamable tracks."
            : undefined
        }
      />
    );
  }
  // Column order: [# | Title | Plays | Album/Artist | action-cluster].
  // The action cluster (heart + duration + download + menu) lives in a
  // single `auto`-sized cell so the buttons can be tightly grouped with
  // their own gap — the parent grid's `gap-4` is too airy for icons
  // meant to read as one related control set.
  // The header grid template MUST match the body row's template — any
  // mismatch lets the flex columns absorb different amounts of space
  // and the icons drift out of alignment between header and body.
  // Plays column gets a FIXED width, not `auto`. The header is one
  // grid and each virtualized body row is its own grid — an `auto`
  // column would size to each grid's content independently, so the
  // "PLAYS" label and the values under it would land at different x
  // positions. A fixed width keeps every grid's column identical so
  // the header and values stay left-aligned to the same edge.
  const gridCols = showPlaycount
    ? "grid-cols-[24px_4fr_56px_3fr_auto]"
    : "grid-cols-[24px_4fr_3fr_auto]";
  const header = (
    <div
      className={cn(
        "grid items-center gap-4 border-b border-border px-4 py-2 text-xs uppercase tracking-wider text-muted-foreground",
        gridCols,
      )}
    >
      <span className="text-center">#</span>
      <span>Title</span>
      {showPlaycount && <span>Plays</span>}
      <span>{showAlbum ? "Album" : "Artist"}</span>
      {/* Mirror the body cluster exactly — same slot widths and gap so
          the Clock icon lands directly above the duration text and all
          four slots read as one evenly-spaced group. */}
      <div className="flex items-center justify-end gap-2">
        <span className="h-8 w-8" aria-hidden />
        <span className="flex w-12 items-center" title="Duration">
          <Clock className="h-4 w-4" />
        </span>
        <span className="h-8 w-8" aria-hidden />
        <span className="h-8 w-8" aria-hidden />
      </div>
    </div>
  );

  // Sortable IDs must be UNIQUE and STABLE across reorders — otherwise
  // dnd-kit can't track the dragged element and React remounts every row
  // on every swap (losing focus, animation state, etc.).
  //
  // Using `track.id` gives us stability: when the array order changes, the
  // id stays with the track. The tradeoff is that playlists with duplicate
  // tracks (rare in practice) will hit dnd-kit's uniqueness invariant. For
  // those, we append a per-occurrence count so the n-th duplicate still
  // gets its own sortable id — accepting that reordering across occurrences
  // isn't perfectly animated in that edge case.
  const rowIds = useMemo(() => {
    const seen = new Map<string, number>();
    return visibleTracks.map((t) => {
      const n = seen.get(t.id) ?? 0;
      seen.set(t.id, n + 1);
      return n === 0 ? t.id : `${t.id}#${n}`;
    });
  }, [visibleTracks]);

  const body = visibleTracks.map((t, idx) => (
    <TrackRow
      key={rowIds[idx]}
      sortableId={rowIds[idx]}
      sortable={!!onReorder}
      track={t}
      index={idx}
      context={visibleTracks}
      numbered={numbered}
      showAlbum={showAlbum}
      showPlaycount={showPlaycount}
      onDownload={onDownload}
      onRemove={onRemove}
    />
  ));

  if (!onReorder) {
    // Gate virtualization on list length — rendering 10 items in full
    // flow is cheaper than setting up virtualizer machinery, and the
    // virtualized path loses some subtle CSS niceties (e.g. natural hover
    // height cross-row). Threshold chosen empirically.
    if (visibleTracks.length > VIRTUALIZE_AT) {
      return (
        <div className="flex flex-col">
          {header}
          <VirtualRows tracks={visibleTracks} rowIds={rowIds}>
            {(idx) => body[idx]}
          </VirtualRows>
        </div>
      );
    }
    return (
      <div className="flex flex-col">
        {header}
        {body}
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      {header}
      <SortableTracks ids={rowIds} tracks={visibleTracks} onReorder={onReorder}>
        {body}
      </SortableTracks>
    </div>
  );
}

// Threshold above which we switch to virtualization. A 100-track album
// renders fine in full flow; a 1000-track Liked Songs list benefits from
// only rendering the ~20 visible rows.
const VIRTUALIZE_AT = 80;
const ROW_HEIGHT_PX = 56; // approximates py-2 + contents

/**
 * Virtualized row renderer. Finds the nearest `data-scroll-container` —
 * the main element set up in App.tsx — and only mounts the rows
 * intersecting its viewport (plus overscan). For a 2000-track Liked Songs
 * list this drops from ~20k rendered components to ~20.
 */
function VirtualRows({
  tracks,
  rowIds,
  children,
}: {
  tracks: Track[];
  rowIds: string[];
  children: (index: number) => React.ReactNode;
}) {
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const [scrollEl, setScrollEl] = useState<HTMLElement | null>(null);

  // useLayoutEffect — the scroll-container lookup + state set must happen
  // before the browser paints, otherwise there's a visible flash where the
  // virtualizer renders nothing (first pass) then rows (second pass).
  useLayoutEffect(() => {
    const el = anchorRef.current?.closest(
      "[data-scroll-container]",
    ) as HTMLElement | null;
    setScrollEl(el);
  }, []);

  const virtualizer = useVirtualizer({
    count: tracks.length,
    getScrollElement: () => scrollEl,
    estimateSize: () => ROW_HEIGHT_PX,
    overscan: 10,
  });

  // Until we've latched onto the scroll element, render nothing —
  // getScrollElement would otherwise return null and the virtualizer
  // logs a warning. The pages already show their own skeleton/loading
  // state while data fetches.
  if (!scrollEl) {
    return <div ref={anchorRef} />;
  }

  const items = virtualizer.getVirtualItems();
  const totalHeight = virtualizer.getTotalSize();

  return (
    <div ref={anchorRef} className="relative" style={{ height: totalHeight }}>
      {items.map((vi) => (
        <div
          key={rowIds[vi.index]}
          data-index={vi.index}
          ref={virtualizer.measureElement}
          className="absolute inset-x-0"
          style={{ transform: `translateY(${vi.start}px)` }}
        >
          {children(vi.index)}
        </div>
      ))}
    </div>
  );
}


function SortableTracks({
  ids,
  tracks,
  onReorder,
  children,
}: {
  ids: string[];
  tracks: Track[];
  onReorder: (mediaId: string, fromIndex: number, toIndex: number) => void;
  children: React.ReactNode;
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (e: DragEndEvent) => {
    const { active, over } = e;
    if (!over || active.id === over.id) return;
    const fromIndex = ids.indexOf(String(active.id));
    const toIndex = ids.indexOf(String(over.id));
    if (fromIndex < 0 || toIndex < 0) return;
    const track = tracks[fromIndex];
    if (!track) return;
    onReorder(track.id, fromIndex, toIndex);
  };

  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  );
}

// arrayMove is re-exported so callers who want to compute an optimistic
// reorder before calling the server can stay independent of dnd-kit.
export { arrayMove };

function TrackRow({
  track,
  index,
  context,
  numbered,
  showAlbum,
  showPlaycount,
  onDownload,
  onRemove,
  sortable,
  sortableId,
}: {
  track: Track;
  index: number;
  context: Track[];
  numbered: boolean;
  showAlbum: boolean;
  showPlaycount: boolean;
  onDownload: OnDownload;
  onRemove?: (index: number) => void;
  sortable?: boolean;
  sortableId?: string;
}) {
  // Subscribe only to the meta slice — time updates (4Hz) don't re-render
  // rows thanks to the PlayerContext split.
  const meta = usePlayerMeta();
  const actions = usePlayerActions();
  const isCurrent = meta.track?.id === track.id;
  const isPlaying = isCurrent && meta.playing;
  const isLoading = isCurrent && meta.loading;
  const isDownloaded = useIsDownloaded(track.id);
  const [creditsOpen, setCreditsOpen] = useState(false);
  const selection = useTrackSelection();
  const isSelected = selection.has(track.id);
  const anySelected = selection.selected.size > 0;

  // useSortable is safe to call unconditionally — outside a SortableContext
  // it returns inert defaults so non-sortable callers render normally.
  const sort = useSortable({ id: sortableId ?? `static-${index}`, disabled: !sortable });
  const sortableStyle = sortable
    ? {
        transform: CSS.Transform.toString(sort.transform),
        transition: sort.transition,
        zIndex: sort.isDragging ? 20 : undefined,
      }
    : undefined;
  const dragHandleProps = sortable ? { ...sort.attributes, ...sort.listeners } : {};

  const onPlayToggle = () => {
    if (isPlaying) {
      actions.toggle();
    } else if (isCurrent) {
      actions.toggle();
    } else {
      actions.play(track, context);
    }
  };

  // Shared props for both menu entry points (right-click and three-dots
  // button) so the two surfaces stay in sync automatically.
  const menuProps = {
    track,
    context,
    onDownload,
    onRemove: onRemove ? () => onRemove(index) : undefined,
    onShowCredits: () => setCreditsOpen(true),
  };

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          ref={sortable ? sort.setNodeRef : undefined}
          style={sortableStyle}
          {...dragHandleProps}
          className={cn(
            "group grid select-none items-center gap-4 rounded-md px-4 py-2 text-sm hover:bg-accent",
            showPlaycount
              ? "grid-cols-[24px_4fr_56px_3fr_auto]"
              : "grid-cols-[24px_4fr_3fr_auto]",
            isCurrent && "bg-accent/60",
            isSelected && "bg-primary/10",
            sortable && "cursor-grab touch-none active:cursor-grabbing",
            sort.isDragging && "bg-accent shadow-lg",
          )}
          onDoubleClick={() => actions.play(track, context)}
        >
          <RowLeadCell
            index={index}
            numbered={numbered}
            isCurrent={isCurrent}
            isPlaying={isPlaying}
            isLoading={isLoading}
            isSelected={isSelected}
            anySelected={anySelected}
            onPlayToggle={onPlayToggle}
            onToggleSelect={(shiftKey) => {
              if (shiftKey && selection.selected.size > 0) {
                // Anchor on the first selected track that exists in this
                // list. If none is in the list, fall back to a plain toggle.
                const firstAnchorId = Array.from(selection.selected.keys()).find(
                  (id) => context.some((t) => t.id === id),
                );
                if (firstAnchorId) {
                  selection.toggleRange(context, firstAnchorId, track.id);
                  return;
                }
              }
              selection.toggle(track);
            }}
          />
          <div className="flex min-w-0 items-center gap-3">
            {showAlbum && track.album?.cover && (
              <img
                src={imageProxy(track.album.cover)}
                alt=""
                className="h-10 w-10 rounded object-cover"
                loading="lazy"
              />
            )}
            <div className="min-w-0">
              <div
                className={cn(
                  "flex items-center gap-2 truncate font-medium",
                  isCurrent ? "text-primary" : "text-foreground",
                )}
              >
                <span className="truncate">{track.name}</span>
                {isDownloaded && (
                  <span
                    title="Downloaded — plays from disk"
                    className="flex-shrink-0 rounded-sm bg-primary/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-primary"
                  >
                    Saved
                  </span>
                )}
              </div>
              <div className="truncate text-xs text-muted-foreground">
                {track.artists.map((a, i) => (
                  <span key={a.id}>
                    {i > 0 && ", "}
                    <Link to={`/artist/${a.id}`} className="hover:underline">
                      {a.name}
                    </Link>
                  </span>
                ))}
              </div>
            </div>
          </div>
          {showPlaycount && (
            <TrackPlaycountCell
              artist={track.artists[0]?.name ?? ""}
              title={track.name}
            />
          )}
          <div className="truncate text-xs text-muted-foreground">
            {showAlbum && track.album ? (
              <Link to={`/album/${track.album.id}`} className="hover:underline">
                {track.album.name}
              </Link>
            ) : (
              track.artists.map((a) => a.name).join(", ")
            )}
          </div>
          {/* Right-side action cluster — heart, duration, download, menu
              sit together with their own tight gap so they read as one
              related control group instead of four separate columns. */}
          <div className="flex items-center justify-end gap-2">
            <div className="flex h-8 w-8 items-center justify-center">
              <HeartButton kind="track" id={track.id} size="sm" visibility="hover" />
            </div>
            <span className="w-12 text-left text-xs tabular-nums text-muted-foreground">
              {formatDuration(track.duration)}
            </span>
            <DownloadButton
              kind="track"
              id={track.id}
              onPick={onDownload}
              iconOnly
              variant="ghost"
              className="h-8 w-8 opacity-0 transition-opacity group-hover:opacity-100 data-[state=open]:opacity-100"
            />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  onClick={(e) => e.stopPropagation()}
                  onPointerDown={(e) => e.stopPropagation()}
                  className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground opacity-0 transition-all hover:bg-accent-foreground/10 hover:text-foreground group-hover:opacity-100 data-[state=open]:opacity-100"
                  title="More"
                  aria-label="Track actions"
                >
                  <MoreHorizontal className="h-4 w-4" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-60">
                <TrackMenuItems parts={DROPDOWN_MENU_PARTS} {...menuProps} />
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <TrackMenuItems parts={CONTEXT_MENU_PARTS} {...menuProps} />
      </ContextMenuContent>
      <CreditsDialog
        trackId={track.id}
        trackName={track.name}
        open={creditsOpen}
        onOpenChange={setCreditsOpen}
      />
    </ContextMenu>
  );
}

/**
 * The leftmost column of a track row — shows the track number, play/pause
 * indicator, or a selection checkbox depending on context.
 *
 * Rules:
 *  - Always checkbox if the row is selected OR anything else is selected
 *    (checkbox mode is "sticky" once engaged so the user can multi-select
 *    by ticking consecutively without losing state on hover out).
 *  - Hover with nothing selected: play/pause icon.
 *  - Otherwise: track number (or empty if numbered=false).
 */
function RowLeadCell({
  index,
  numbered,
  isCurrent,
  isPlaying,
  isLoading,
  isSelected,
  anySelected,
  onPlayToggle,
  onToggleSelect,
}: {
  index: number;
  numbered: boolean;
  isCurrent: boolean;
  isPlaying: boolean;
  isLoading: boolean;
  isSelected: boolean;
  anySelected: boolean;
  onPlayToggle: () => void;
  onToggleSelect: (shiftKey: boolean) => void;
}) {
  // Stop pointer events from bubbling to the row — otherwise the drag
  // sensor on sortable rows would interpret a click-and-drag over these
  // buttons as a drag start after 6px of movement, stealing the click.
  const stopPointerDown = (e: React.PointerEvent) => e.stopPropagation();

  if (anySelected) {
    return (
      <button
        onPointerDown={stopPointerDown}
        onClick={(e) => {
          e.stopPropagation();
          onToggleSelect(e.shiftKey);
        }}
        className="flex h-6 w-6 items-center justify-center"
        aria-label={isSelected ? "Deselect track" : "Select track"}
        aria-pressed={isSelected}
      >
        <span
          className={cn(
            "flex h-4 w-4 items-center justify-center rounded border text-primary-foreground transition-colors",
            isSelected
              ? "border-primary bg-primary"
              : "border-muted-foreground/50 bg-transparent hover:border-foreground",
          )}
        >
          {isSelected && <Check className="h-3 w-3" />}
        </span>
      </button>
    );
  }

  // Default: play/pause on hover, number/empty otherwise.
  return (
    <button
      onPointerDown={stopPointerDown}
      onClick={onPlayToggle}
      className="group/play relative flex h-6 w-6 items-center justify-center text-muted-foreground hover:text-foreground"
      title={isPlaying ? "Pause preview" : "Play preview"}
    >
      <span
        className={cn(
          "text-xs",
          isCurrent ? "hidden" : "inline group-hover:hidden",
        )}
      >
        {numbered ? index + 1 : ""}
      </span>
      <span
        className={cn(
          "items-center justify-center",
          isCurrent ? "flex" : "hidden group-hover:flex",
        )}
      >
        {isLoading ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
        ) : isPlaying ? (
          <Pause className="h-3.5 w-3.5 text-primary" fill="currentColor" />
        ) : (
          <Play className="h-3.5 w-3.5" fill="currentColor" />
        )}
      </span>
    </button>
  );
}

/**
 * Right-aligned compact playcount in its own column. Fires a Last.fm
 * `track.getInfo` via the shared module-level cache so the same track
 * doesn't refetch across surfaces. Returns an empty cell (to preserve
 * grid alignment) when Last.fm isn't configured or the track has no
 * reported plays — we don't want "0 plays" ghost text on every row.
 */
function TrackPlaycountCell({ artist, title }: { artist: string; title: string }) {
  const pc = useLastfmTrackPlaycount(artist, title);
  const count = pc?.playcount ?? 0;
  if (count <= 0) {
    return <span />;
  }
  return (
    <span
      className="text-xs tabular-nums text-muted-foreground"
      title={`${count.toLocaleString()} plays on Last.fm${
        pc?.listeners ? ` · ${pc.listeners.toLocaleString()} listeners` : ""
      }`}
    >
      {formatCompact(count)}
    </span>
  );
}

function formatCompact(n: number): string {
  if (n < 1000) return n.toLocaleString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

