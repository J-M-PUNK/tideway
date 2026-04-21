import { Heart, Loader2, Plus } from "lucide-react";
import { api } from "@/api/client";
import type { Track } from "@/api/types";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { useToast } from "@/components/toast";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";

/**
 * Labeled "Add" button for collections without a favorite concept
 * (mixes). Visually matches `AddToLibraryButton` (heart + "Add" below)
 * but clicking opens a playlist picker instead of toggling favorite
 * state. Bulk-adds every track in the collection to the chosen
 * playlist in one `addTracks` call.
 */
export function AddTracksToPlaylistButton({ tracks }: { tracks: Track[] }) {
  const { playlists } = useMyPlaylists();
  const toast = useToast();

  const add = async (playlistId: string, playlistName: string) => {
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

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          disabled={tracks.length === 0}
          className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
          title={`Add ${tracks.length} tracks to a playlist`}
          aria-label="Add"
        >
          <Heart className="h-5 w-5" />
          <div className="text-xs font-semibold">Add</div>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-96 w-64 overflow-y-auto">
        <DropdownMenuLabel>Add to playlist</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <CreatePlaylistDialog
          trigger={
            <button className="flex w-full cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent">
              <Plus className="h-3.5 w-3.5" /> New playlist…
            </button>
          }
        />
        {playlists.length > 0 && <DropdownMenuSeparator />}
        {playlists.length === 0 && (
          <DropdownMenuItem disabled>
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
          </DropdownMenuItem>
        )}
        {playlists.map((p) => (
          <DropdownMenuItem key={p.id} onSelect={() => add(p.id, p.name)}>
            <span className="truncate">{p.name}</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
