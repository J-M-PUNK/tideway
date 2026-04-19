import { Link } from "react-router-dom";
import {
  Check,
  CheckSquare,
  Clock,
  Copy,
  Disc3,
  FileText,
  ListPlus,
  Loader2,
  Music,
  Pause,
  Play,
  Plus,
  Radio,
  Trash2,
  User,
} from "lucide-react";
import type { Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { formatDuration, imageProxy } from "@/lib/utils";
import { DownloadButton } from "@/components/DownloadButton";
import { EmptyState } from "@/components/EmptyState";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { useDownloadedIds, useIsDownloaded } from "@/hooks/useDownloadedSet";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";
import { useFavorites } from "@/hooks/useFavorites";
import { useTrackSelection } from "@/hooks/useTrackSelection";
import { useUiPreferences } from "@/hooks/useUiPreferences";
import { HeartButton } from "@/components/HeartButton";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { CreditsDialog } from "@/components/CreditsDialog";
import { useToast } from "@/components/toast";
import { cn } from "@/lib/utils";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
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
import { api } from "@/api/client";
import type { QualityOption } from "@/api/types";

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
}

export function TrackList({
  tracks,
  onDownload,
  showAlbum = true,
  numbered = true,
  onRemove,
  onReorder,
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
  const header = (
    <div className="grid grid-cols-[24px_4fr_3fr_48px_40px_40px] items-center gap-4 border-b border-border px-4 py-2 text-xs uppercase tracking-wider text-muted-foreground">
      <span className="text-center">#</span>
      <span>Title</span>
      <span>{showAlbum ? "Album" : "Artist"}</span>
      <Clock className="h-4 w-4 justify-self-end" />
      <span />
      <span />
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
  onDownload: OnDownload;
  onRemove?: (index: number) => void;
  sortable?: boolean;
  sortableId?: string;
}) {
  const toast = useToast();
  const favs = useFavorites();
  // Subscribe only to the meta slice — time updates (4Hz) don't re-render
  // rows thanks to the PlayerContext split.
  const meta = usePlayerMeta();
  const actions = usePlayerActions();
  const isCurrent = meta.track?.id === track.id;
  const isPlaying = isCurrent && meta.playing;
  const isLoading = isCurrent && meta.loading;
  const isDownloaded = useIsDownloaded(track.id);
  const liked = favs.has("track", track.id);
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

  const copyTidalLink = async () => {
    const url = `https://tidal.com/browse/track/${track.id}`;
    try {
      await navigator.clipboard.writeText(url);
      toast.show({ kind: "success", title: "Link copied", description: url });
    } catch {
      toast.show({ kind: "error", title: "Copy failed", description: "Clipboard not available." });
    }
  };

  const startRadio = async () => {
    try {
      const radio = await api.trackRadio(track.id);
      if (!radio.length) {
        toast.show({
          kind: "info",
          title: "No radio",
          description: "Tidal doesn't have a radio for this track.",
        });
        return;
      }
      // Play the seed track first, then append the radio selections so
      // playback continues organically when the seed ends.
      actions.play(track, [track, ...radio]);
      toast.show({ kind: "success", title: "Radio started", description: `${radio.length} tracks queued` });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start radio",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          ref={sortable ? sort.setNodeRef : undefined}
          style={sortableStyle}
          {...dragHandleProps}
          className={cn(
            "group grid grid-cols-[24px_4fr_3fr_48px_40px_40px] items-center gap-4 rounded-md px-4 py-2 text-sm hover:bg-accent",
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
          <div className="truncate text-xs text-muted-foreground">
            {showAlbum && track.album ? (
              <Link to={`/album/${track.album.id}`} className="hover:underline">
                {track.album.name}
              </Link>
            ) : (
              track.artists.map((a) => a.name).join(", ")
            )}
          </div>
          <span className="justify-self-end text-xs text-muted-foreground">
            {formatDuration(track.duration)}
          </span>
          <HeartButton kind="track" id={track.id} size="sm" visibility="hover" />
          <DownloadButton
            kind="track"
            id={track.id}
            onPick={onDownload}
            iconOnly
            variant="ghost"
            className="h-8 w-8 opacity-0 transition-opacity group-hover:opacity-100 data-[state=open]:opacity-100"
          />
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={() => actions.play(track, context)}>
          <Play className="h-3.5 w-3.5" /> Play
        </ContextMenuItem>
        <ContextMenuItem onSelect={() => actions.playNext(track)}>
          <ListPlus className="h-3.5 w-3.5" /> Play next
        </ContextMenuItem>
        <ContextMenuItem onSelect={startRadio}>
          <Radio className="h-3.5 w-3.5" /> Start radio
        </ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={() => selection.toggle(track)}>
          <CheckSquare
            className={cn("h-3.5 w-3.5", isSelected && "text-primary")}
          />
          {isSelected ? "Deselect" : "Select"}
        </ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={() => favs.toggle("track", track.id)}>
          <Check
            className={cn("h-3.5 w-3.5", liked ? "text-primary" : "opacity-0")}
          />
          {liked ? "Remove from Liked Songs" : "Add to Liked Songs"}
        </ContextMenuItem>
        <AddToPlaylistSubmenu trackId={track.id} trackName={track.name} />
        <ContextMenuSeparator />
        <DownloadSubmenu
          onPick={(quality) => onDownload("track", track.id, quality)}
        />
        <ContextMenuSeparator />
        {track.album && (
          <ContextMenuItem asChild>
            <Link to={`/album/${track.album.id}`}>
              <Disc3 className="h-3.5 w-3.5" /> Go to album
            </Link>
          </ContextMenuItem>
        )}
        {track.artists[0] && (
          <ContextMenuItem asChild>
            <Link to={`/artist/${track.artists[0].id}`}>
              <User className="h-3.5 w-3.5" /> Go to artist
            </Link>
          </ContextMenuItem>
        )}
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={() => setCreditsOpen(true)}>
          <FileText className="h-3.5 w-3.5" /> Credits…
        </ContextMenuItem>
        <ContextMenuItem onSelect={copyTidalLink}>
          <Copy className="h-3.5 w-3.5" /> Copy Tidal link
        </ContextMenuItem>
        {onRemove && (
          <>
            <ContextMenuSeparator />
            <ContextMenuItem onSelect={() => onRemove(index)} className="text-destructive">
              <Trash2 className="h-3.5 w-3.5" /> Remove from playlist
            </ContextMenuItem>
          </>
        )}
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

function AddToPlaylistSubmenu({
  trackId,
  trackName,
}: {
  trackId: string;
  trackName: string;
}) {
  const { playlists } = useMyPlaylists();
  const toast = useToast();

  const add = async (playlistId: string, playlistName: string) => {
    try {
      await api.playlists.addTracks(playlistId, [trackId]);
      toast.show({
        kind: "success",
        title: "Added to playlist",
        description: `"${trackName}" → ${playlistName}`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't add to playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <ContextMenuSub>
      <ContextMenuSubTrigger>
        <Plus className="h-3.5 w-3.5" /> Add to playlist…
      </ContextMenuSubTrigger>
      <ContextMenuSubContent className="max-h-96 overflow-y-auto">
        <CreatePlaylistDialog
          trigger={
            <button className="flex w-full cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent">
              <Plus className="h-3.5 w-3.5" /> New playlist…
            </button>
          }
        />
        {playlists.length > 0 && <ContextMenuSeparator />}
        {playlists.map((p) => (
          <ContextMenuItem key={p.id} onSelect={() => add(p.id, p.name)}>
            <span className="truncate">{p.name}</span>
          </ContextMenuItem>
        ))}
        {playlists.length === 0 && (
          <div className="px-3 py-2 text-xs text-muted-foreground">
            No playlists yet. Create one above.
          </div>
        )}
      </ContextMenuSubContent>
    </ContextMenuSub>
  );
}

function DownloadSubmenu({ onPick }: { onPick: (quality?: string) => void }) {
  const [qualities, setQualities] = useState<QualityOption[]>([]);
  useEffect(() => {
    api.qualities().then(setQualities).catch(() => setQualities([]));
  }, []);
  return (
    <ContextMenuSub>
      <ContextMenuSubTrigger>Download…</ContextMenuSubTrigger>
      <ContextMenuSubContent>
        <ContextMenuItem onSelect={() => onPick()}>Use default quality</ContextMenuItem>
        <ContextMenuSeparator />
        {qualities.map((q) => (
          <ContextMenuItem key={q.value} onSelect={() => onPick(q.value)}>
            <div className="flex flex-col">
              <span>
                {q.label} · {q.codec}
              </span>
              <span className="text-[11px] text-muted-foreground">{q.bitrate}</span>
            </div>
          </ContextMenuItem>
        ))}
      </ContextMenuSubContent>
    </ContextMenuSub>
  );
}
