import { Link } from "react-router-dom";
import {
  Check,
  Clock,
  Copy,
  Disc3,
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
import { useIsDownloaded } from "@/hooks/useDownloadedSet";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";
import { useFavorites } from "@/hooks/useFavorites";
import { HeartButton } from "@/components/HeartButton";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
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
import { useEffect, useState } from "react";
import {
  DndContext,
  closestCenter,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
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
  if (tracks.length === 0) {
    return <EmptyState icon={Music} title="No tracks" />;
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
  const rowIds = (() => {
    const seen = new Map<string, number>();
    return tracks.map((t) => {
      const n = seen.get(t.id) ?? 0;
      seen.set(t.id, n + 1);
      return n === 0 ? t.id : `${t.id}#${n}`;
    });
  })();

  const body = tracks.map((t, idx) => (
    <TrackRow
      key={rowIds[idx]}
      sortableId={rowIds[idx]}
      sortable={!!onReorder}
      track={t}
      index={idx}
      context={tracks}
      numbered={numbered}
      showAlbum={showAlbum}
      onDownload={onDownload}
      onRemove={onRemove}
    />
  ));

  if (!onReorder) {
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
      <SortableTracks ids={rowIds} tracks={tracks} onReorder={onReorder}>
        {body}
      </SortableTracks>
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
            sortable && "cursor-grab touch-none active:cursor-grabbing",
            sort.isDragging && "bg-accent shadow-lg",
          )}
          onDoubleClick={() => actions.play(track, context)}
        >
          <button
            onClick={onPlayToggle}
            className="flex h-6 w-6 items-center justify-center text-muted-foreground hover:text-foreground"
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
    </ContextMenu>
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
