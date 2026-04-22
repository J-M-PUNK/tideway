import { Download, ListPlus, MoreHorizontal, Plus } from "lucide-react";
import { api } from "@/api/client";
import type { ContentKind, Track } from "@/api/types";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useToast } from "@/components/toast";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";

/**
 * Three-dots overflow menu for a detail page (mix / album / playlist).
 * Keeps the core actions (Play next, Add to playlist, Download)
 * consistent across all collection types so users don't relearn the
 * menu per page.
 *
 * - **Play next**: inserts all tracks directly after the current in
 *   the queue, preserving their order. No-op when the collection is
 *   empty.
 * - **Add to playlist**: submenu listing the user's playlists; picking
 *   one bulk-adds all tracks via a single `addTracks` call.
 * - **Download**: enqueues the whole collection as a bulk job. When
 *   a `downloadKind`+`downloadId` is provided we use that (server-
 *   side "download whole album" behavior, respects settings); otherwise
 *   we fall back to enqueuing per-track.
 */
interface Props {
  tracks: Track[];
  /** Collection-level download target (e.g. "album", "playlist"). When
   *  omitted or no matching ID, we fall back to per-track. */
  downloadKind?: Extract<ContentKind, "album" | "playlist">;
  downloadId?: string;
}

export function CollectionOverflowMenu({ tracks, downloadKind, downloadId }: Props) {
  const toast = useToast();
  const actions = usePlayerActions();
  const { playlists } = useMyPlaylists();

  const playNext = () => {
    if (tracks.length === 0) return;
    // playNext inserts at `queueIndex + 1`. Walking the list in reverse
    // so each insert lands ahead of the previous, leaving the final
    // order as: [current, tracks[0], tracks[1], …, tracks[N-1], …prev].
    for (let i = tracks.length - 1; i >= 0; i--) {
      actions.playNext(tracks[i]);
    }
    toast.show({
      kind: "success",
      title: `Queued ${tracks.length} ${tracks.length === 1 ? "track" : "tracks"} next`,
    });
  };

  const addToPlaylist = async (playlistId: string, playlistName: string) => {
    if (tracks.length === 0) return;
    try {
      await api.playlists.addTracks(
        playlistId,
        tracks.map((t) => t.id),
      );
      toast.show({
        kind: "success",
        title: `Added ${tracks.length} ${tracks.length === 1 ? "track" : "tracks"}`,
        description: `→ ${playlistName}`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't add to playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const download = async () => {
    if (tracks.length === 0) return;
    try {
      if (downloadKind && downloadId) {
        await api.downloads.enqueueBulk([{ kind: downloadKind, id: downloadId }]);
      } else {
        await api.downloads.enqueueBulk(
          tracks.map((t) => ({ kind: "track" as const, id: t.id })),
        );
      }
      toast.show({
        kind: "success",
        title: "Queued for download",
        description: "Running in the background.",
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't download",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground data-[state=open]:text-primary"
          title="More"
          aria-label="More actions"
        >
          <MoreHorizontal className="h-5 w-5" />
          <div className="text-xs font-semibold">More</div>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem onSelect={playNext}>
          <ListPlus className="h-3.5 w-3.5" /> Play next
        </DropdownMenuItem>
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <Plus className="h-3.5 w-3.5" /> Add to playlist
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="max-h-96 w-56 overflow-y-auto">
            <CreatePlaylistDialog
              trigger={
                <button className="flex w-full cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent">
                  <Plus className="h-3.5 w-3.5" /> New playlist…
                </button>
              }
            />
            {playlists.length > 0 && <DropdownMenuSeparator />}
            {playlists.map((p) => (
              <DropdownMenuItem key={p.id} onSelect={() => addToPlaylist(p.id, p.name)}>
                <span className="truncate">{p.name}</span>
              </DropdownMenuItem>
            ))}
            {playlists.length === 0 && (
              <div className="px-3 py-2 text-xs text-muted-foreground">
                No playlists yet. Create one above.
              </div>
            )}
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={download}>
          <Download className="h-3.5 w-3.5" /> Download
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
