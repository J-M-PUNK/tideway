import { useState } from "react";
import { Download, Heart, Loader2, ListPlus, X } from "lucide-react";
import { api } from "@/api/client";
import { useTrackSelection } from "@/hooks/useTrackSelection";
import { useFavorites } from "@/hooks/useFavorites";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";
import { useToast } from "@/components/toast";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * Floating bar that appears when the user has selected one or more tracks.
 * Sits above the Now Playing bar. Collapses (returns null) when empty.
 */
export function SelectionBar() {
  const { selected, clear, removeMany } = useTrackSelection();
  const count = selected.size;
  const [busy, setBusy] = useState<null | "download" | "like" | "playlist">(
    null,
  );
  const toast = useToast();
  const favs = useFavorites();
  const { playlists } = useMyPlaylists();

  if (count === 0) return null;

  const downloadAll = async () => {
    // Snapshot ids at action start — the await that follows may see the
    // user add or remove items from the selection, and we want to submit
    // and clear exactly what they clicked on, not a drifting target.
    const ids = Array.from(selected.keys());
    setBusy("download");
    try {
      const res = await api.downloads.enqueueBulk(
        ids.map((id) => ({ kind: "track" as const, id })),
      );
      toast.show({
        kind: "success",
        title: `Queueing ${res.submitted} tracks`,
        description: "Running in the background.",
      });
      removeMany(ids);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Bulk download failed",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  };

  const likeAll = async () => {
    const ids = Array.from(selected.keys());
    setBusy("like");
    try {
      // Skip tracks already liked so we don't spam the API with no-ops.
      const toLike = ids.filter((id) => !favs.has("track", id));
      if (toLike.length === 0) {
        toast.show({ kind: "info", title: "Already liked" });
        return;
      }
      // Use the server-side bulk endpoint — avoids fanning out N parallel
      // requests (which would instantly trip Tidal's rate limit for large
      // batches). Server runs the writes sequentially in a background
      // thread and returns immediately.
      await api.favorites.bulk("track", toLike, true);
      toast.show({
        kind: "success",
        title: `Liking ${toLike.length} ${toLike.length === 1 ? "track" : "tracks"}`,
        description:
          toLike.length < ids.length
            ? `Skipped ${ids.length - toLike.length} already liked. Running in the background.`
            : "Running in the background.",
      });
      removeMany(ids);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't like tracks",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  };

  const addToPlaylist = async (playlistId: string, playlistName: string) => {
    const ids = Array.from(selected.keys());
    setBusy("playlist");
    try {
      await api.playlists.addTracks(playlistId, ids);
      toast.show({
        kind: "success",
        title: `Added ${ids.length} tracks`,
        description: `→ ${playlistName}`,
      });
      removeMany(ids);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't add to playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="pointer-events-auto border-t border-border bg-primary/90 px-6 py-3 text-primary-foreground backdrop-blur-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold">
            {count} {count === 1 ? "track" : "tracks"} selected
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={clear}
            className="h-7 text-primary-foreground/80 hover:bg-black/10 hover:text-primary-foreground"
          >
            <X className="h-3.5 w-3.5" /> Clear
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="secondary"
            onClick={downloadAll}
            disabled={busy !== null}
          >
            {busy === "download" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5" />
            )}
            Download
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={likeAll}
            disabled={busy !== null}
          >
            {busy === "like" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Heart className="h-3.5 w-3.5" />
            )}
            Like
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="secondary" disabled={busy !== null}>
                <ListPlus className="h-3.5 w-3.5" /> Add to playlist
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="max-h-80 overflow-y-auto"
            >
              <DropdownMenuLabel>Choose playlist</DropdownMenuLabel>
              <DropdownMenuSeparator />
              {playlists.length === 0 && (
                <div className="px-3 py-2 text-xs text-muted-foreground">
                  No playlists yet.
                </div>
              )}
              {playlists.map((p) => (
                <DropdownMenuItem
                  key={p.id}
                  onSelect={() => addToPlaylist(p.id, p.name)}
                >
                  <span className="truncate">{p.name}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </div>
  );
}
