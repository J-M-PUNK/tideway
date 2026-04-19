import { useCallback, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Loader2, Pencil, Trash2 } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import type { Track } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";
import { useToast } from "@/components/toast";
import { DetailHero } from "@/components/DetailHero";
import { DownloadButton } from "@/components/DownloadButton";
import { HeartButton } from "@/components/HeartButton";
import { PlayAllButton } from "@/components/PlayAllButton";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { formatDuration } from "@/lib/utils";

export function PlaylistDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  // refreshTick is bumped after edits to re-run the playlist fetch without
  // reloading the whole SPA (which would nuke queue, scroll, player state).
  const [refreshTick, setRefreshTick] = useState(0);
  const { data: playlist, loading, error } = useApi(
    () => api.playlist(id),
    [id, refreshTick],
  );
  // Local optimistic copy of tracks so removing a track feels instant.
  const [localTracks, setLocalTracks] = useState<Track[] | null>(null);
  const toast = useToast();

  const tracks = localTracks ?? playlist?.tracks ?? [];

  // Serialize reorder/remove mutations. Every operation sends a
  // 0-based index that refers to the server's live list — firing two in
  // parallel makes the second one's index reference a list state that no
  // longer exists, causing the wrong track to be deleted or moved. Also
  // protects against the local-tracks state tearing across awaits when
  // the user drags/removes rapidly.
  const mutatingRef = useRef(false);

  const onRemove = useCallback(
    async (index: number) => {
      if (!playlist) return;
      if (mutatingRef.current) return;
      mutatingRef.current = true;
      const prev = tracks;
      const next = [...prev];
      const [removed] = next.splice(index, 1);
      setLocalTracks(next);
      try {
        await api.playlists.removeTrack(playlist.id, index);
        toast.show({
          kind: "success",
          title: "Removed from playlist",
          description: removed.name,
        });
      } catch (err) {
        setLocalTracks(prev);
        toast.show({
          kind: "error",
          title: "Couldn't remove",
          description: err instanceof Error ? err.message : String(err),
        });
      } finally {
        mutatingRef.current = false;
      }
    },
    [playlist, tracks, toast],
  );

  const onReorder = useCallback(
    async (mediaId: string, fromIndex: number, toIndex: number) => {
      if (!playlist) return;
      if (mutatingRef.current) return;
      mutatingRef.current = true;
      const prev = tracks;
      // Optimistic: reorder locally so the row visibly snaps into place.
      const next = [...prev];
      const [moved] = next.splice(fromIndex, 1);
      next.splice(toIndex, 0, moved);
      setLocalTracks(next);
      try {
        await api.playlists.moveTrack(playlist.id, mediaId, toIndex);
      } catch (err) {
        setLocalTracks(prev);
        toast.show({
          kind: "error",
          title: "Couldn't reorder",
          description: err instanceof Error ? err.message : String(err),
        });
      } finally {
        mutatingRef.current = false;
      }
    },
    [playlist, tracks, toast],
  );

  if (loading) {
    return (
      <div>
        <HeroSkeleton />
        <div className="mt-10">
          <TrackListSkeleton />
        </div>
      </div>
    );
  }
  if (error || !playlist) return <ErrorView error={error ?? "Playlist not found"} />;

  return (
    <div>
      <DetailHero
        eyebrow={playlist.owned ? "Your playlist" : "Playlist"}
        title={playlist.name}
        cover={playlist.cover}
        meta={
          <div className="flex flex-col gap-2">
            {playlist.description && (
              <p className="line-clamp-2 text-muted-foreground">{playlist.description}</p>
            )}
            <span>
              {playlist.creator ? `By ${playlist.creator} · ` : ""}
              {tracks.length} tracks · {formatDuration(playlist.duration)}
            </span>
          </div>
        }
        actions={
          <>
            {tracks.length > 0 && <PlayAllButton tracks={tracks} />}
            <DownloadButton
              kind="playlist"
              id={playlist.id}
              onPick={onDownload}
              size="lg"
              label="Download playlist"
            />
            {!playlist.owned && <HeartButton kind="playlist" id={playlist.id} />}
            {playlist.owned && (
              <>
                <EditPlaylistButton
                  playlistId={playlist.id}
                  initialTitle={playlist.name}
                  initialDescription={playlist.description}
                  onSaved={() => setRefreshTick((n) => n + 1)}
                />
                <DeletePlaylistButton playlistId={playlist.id} name={playlist.name} />
              </>
            )}
          </>
        }
      />
      <div className="mt-8">
        <TrackList
          tracks={tracks}
          onDownload={onDownload}
          onRemove={playlist.owned ? onRemove : undefined}
          onReorder={playlist.owned ? onReorder : undefined}
        />
      </div>
    </div>
  );
}

function EditPlaylistButton({
  playlistId,
  initialTitle,
  initialDescription,
  onSaved,
}: {
  playlistId: string;
  initialTitle: string;
  initialDescription: string;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState(initialTitle);
  const [description, setDescription] = useState(initialDescription);
  const [submitting, setSubmitting] = useState(false);
  const toast = useToast();
  const { refresh } = useMyPlaylists();

  const save = async () => {
    setSubmitting(true);
    try {
      await api.playlists.edit(playlistId, { title: title.trim(), description });
      toast.show({ kind: "success", title: "Playlist updated" });
      setOpen(false);
      refresh().catch(() => {});
      // Refetch this page instead of reloading the whole SPA — keeps the
      // current queue, playback position, and scroll intact.
      onSaved();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't save",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Button variant="outline" size="lg" onClick={() => setOpen(true)}>
        <Pencil className="h-4 w-4" /> Edit
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit playlist</DialogTitle>
            <DialogDescription>Update the name or description.</DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
            className="flex flex-col gap-4"
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-title">Name</Label>
              <Input
                id="edit-title"
                autoFocus
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={200}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-desc">Description</Label>
              <Input
                id="edit-desc"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                maxLength={500}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!title.trim() || submitting}>
                {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
                Save
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

function DeletePlaylistButton({ playlistId, name }: { playlistId: string; name: string }) {
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const toast = useToast();
  const navigate = useNavigate();
  const { optimisticRemove } = useMyPlaylists();

  const confirmDelete = async () => {
    setSubmitting(true);
    try {
      await api.playlists.delete(playlistId);
      optimisticRemove(playlistId);
      toast.show({ kind: "success", title: "Playlist deleted", description: name });
      navigate("/library/playlists");
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't delete playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
      setOpen(false);
    }
  };

  return (
    <>
      <Button variant="outline" size="lg" onClick={() => setOpen(true)}>
        <Trash2 className="h-4 w-4" /> Delete
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete "{name}"?</DialogTitle>
            <DialogDescription>
              This removes the playlist from your Tidal account. It can't be undone.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmDelete} disabled={submitting}>
              {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
              Delete
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
