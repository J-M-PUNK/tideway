import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  Folder,
  FolderMinus,
  Loader2,
  Pencil,
  Trash2,
} from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import type { PlaylistFolder, Track } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";
import { useTrackPrefetch } from "@/hooks/useTrackPrefetch";
import { useToast } from "@/components/toast";
import { AddToLibraryButton } from "@/components/AddToLibraryButton";
import { CollectionOverflowMenu } from "@/components/CollectionOverflowMenu";
import { DetailHero } from "@/components/DetailHero";
import { ShareButton } from "@/components/ShareButton";
import { ShuffleButton } from "@/components/ShuffleButton";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import {
  computeDuplicateGroups,
  DuplicatesPanel,
} from "@/components/DuplicatesPanel";
import { formatDuration } from "@/lib/utils";

export function PlaylistDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  // refreshTick is bumped after edits to re-run the playlist fetch without
  // reloading the whole SPA (which would nuke queue, scroll, player state).
  const [refreshTick, setRefreshTick] = useState(0);
  const {
    data: playlist,
    loading,
    error,
  } = useApi(() => api.playlist(id), [id, refreshTick]);
  // Local optimistic copy of tracks so removing a track feels instant.
  const [localTracks, setLocalTracks] = useState<Track[] | null>(null);
  const toast = useToast();

  const tracks = localTracks ?? playlist?.tracks ?? [];

  // Warm the stream-manifest cache for every track on the playlist so
  // a click on any row skips the Tidal playbackinfo round-trip. Only
  // re-fires when the track list identity changes, not on refreshTick
  // or local optimistic edits.
  const { prefetchMany } = useTrackPrefetch();
  useEffect(() => {
    if (playlist?.tracks?.length) {
      prefetchMany(playlist.tracks.map((t) => t.id));
    }
  }, [playlist?.tracks, prefetchMany]);

  // Content-keyed duplicate detection. Only meaningful on playlists
  // the user owns (others' playlists aren't editable). We recompute
  // on every `tracks` change; it's O(n) and playlists rarely exceed a
  // few hundred rows.
  const duplicates = useMemo(
    () => (playlist?.owned ? computeDuplicateGroups(tracks) : []),
    [tracks, playlist?.owned],
  );
  const [duplicatesOpen, setDuplicatesOpen] = useState(false);
  // Local shuffle pre-selection for this playlist. See AlbumDetail
  // for the pattern; nothing happens to global player state until
  // the user presses Play on this page.
  const [shuffleIntent, setShuffleIntent] = useState(false);
  const dupeTrackCount = duplicates.reduce(
    (s, g) => s + (g.indices.length - 1),
    0,
  );

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
  if (error || !playlist)
    return <ErrorView error={error ?? "Playlist not found"} />;

  return (
    <div>
      <DetailHero
        eyebrow={playlist.owned ? "Your playlist" : "Playlist"}
        title={playlist.name}
        cover={playlist.cover}
        meta={
          <div className="flex flex-col gap-2">
            {playlist.description && (
              <p className="line-clamp-2 text-muted-foreground">
                {playlist.description}
              </p>
            )}
            <span>
              {playlist.creator && (
                <>
                  By{" "}
                  {playlist.creator_id && playlist.creator_id !== "0" ? (
                    <Link
                      to={`/user/${playlist.creator_id}`}
                      className="font-semibold text-foreground hover:underline"
                    >
                      {playlist.creator}
                    </Link>
                  ) : (
                    <span className="font-semibold text-foreground">
                      {playlist.creator}
                    </span>
                  )}{" "}
                  ·{" "}
                </>
              )}
              {tracks.length} tracks · {formatDuration(playlist.duration)}
            </span>
          </div>
        }
        actions={
          <>
            {tracks.length > 0 && (
              <PlayAllButton
                tracks={tracks}
                source={{ type: "PLAYLIST", id: playlist.id }}
                shuffleIntent={shuffleIntent}
              />
            )}
            {tracks.length > 0 && (
              <ShuffleButton
                value={shuffleIntent}
                onChange={setShuffleIntent}
              />
            )}
            <div className="ml-auto flex items-center gap-6">
              {!playlist.owned && (
                <AddToLibraryButton kind="playlist" id={playlist.id} />
              )}
              <ShareButton shareUrl={playlist.share_url} />
              <CollectionOverflowMenu
                tracks={tracks}
                downloadKind="playlist"
                downloadId={playlist.id}
              />
              {playlist.owned && (
                <MoveToFolderButton playlistId={playlist.id} />
              )}
              {playlist.owned && (
                <>
                  <EditPlaylistButton
                    playlistId={playlist.id}
                    initialTitle={playlist.name}
                    initialDescription={playlist.description}
                    onSaved={() => setRefreshTick((n) => n + 1)}
                  />
                  <DeletePlaylistButton
                    playlistId={playlist.id}
                    name={playlist.name}
                  />
                </>
              )}
            </div>
          </>
        }
      />
      {dupeTrackCount > 0 && (
        <button
          type="button"
          onClick={() => setDuplicatesOpen(true)}
          className="mt-6 flex w-full items-center gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-left text-sm text-amber-200 transition-colors hover:bg-amber-500/15"
        >
          <AlertTriangle className="h-4 w-4 flex-shrink-0" />
          <span className="flex-1">
            This playlist has {dupeTrackCount} duplicate{" "}
            {dupeTrackCount === 1 ? "track" : "tracks"} across{" "}
            {duplicates.length} song{duplicates.length === 1 ? "" : "s"}.
          </span>
          <span className="text-xs font-semibold uppercase tracking-wider">
            Review
          </span>
        </button>
      )}

      <div className="mt-8">
        <TrackList
          tracks={tracks}
          onDownload={onDownload}
          onRemove={playlist.owned ? onRemove : undefined}
          onReorder={playlist.owned ? onReorder : undefined}
          source={{ type: "PLAYLIST", id: playlist.id }}
        />
      </div>

      <DuplicatesPanel
        open={duplicatesOpen}
        onOpenChange={setDuplicatesOpen}
        playlistId={playlist.id}
        groups={duplicates}
        onRemoved={() => {
          setLocalTracks(null);
          setRefreshTick((n) => n + 1);
        }}
      />
    </div>
  );
}

function MoveToFolderButton({ playlistId }: { playlistId: string }) {
  const toast = useToast();
  const [folders, setFolders] = useState<PlaylistFolder[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.library.folders
      .list("root")
      .then((f) => {
        if (!cancelled) setFolders(f);
      })
      .catch(() => {
        if (!cancelled) setFolders([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const move = async (folderId: string, folderName: string) => {
    try {
      await api.library.folders.movePlaylists(folderId, [playlistId]);
      toast.show({
        kind: "success",
        title:
          folderId === "root"
            ? "Moved out of folder"
            : `Moved to ${folderName}`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't move playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline">
          <Folder className="h-4 w-4" /> Move
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel>Move to folder</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {folders === null && (
          <DropdownMenuItem disabled>
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
          </DropdownMenuItem>
        )}
        {folders && folders.length === 0 && (
          <DropdownMenuItem disabled>
            No folders yet. Create one on the Playlists page.
          </DropdownMenuItem>
        )}
        {folders?.map((f) => (
          <DropdownMenuItem key={f.id} onSelect={() => move(f.id, f.name)}>
            <Folder className="h-3.5 w-3.5" /> {f.name}
          </DropdownMenuItem>
        ))}
        {folders && folders.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => move("root", "root")}>
              <FolderMinus className="h-3.5 w-3.5" /> Remove from folder
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
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
      await api.playlists.edit(playlistId, {
        title: title.trim(),
        description,
      });
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
      <Button variant="outline" onClick={() => setOpen(true)}>
        <Pencil className="h-4 w-4" /> Edit
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit playlist</DialogTitle>
            <DialogDescription>
              Update the name or description.
            </DialogDescription>
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
              <Button
                type="button"
                variant="ghost"
                onClick={() => setOpen(false)}
              >
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

function DeletePlaylistButton({
  playlistId,
  name,
}: {
  playlistId: string;
  name: string;
}) {
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
      toast.show({
        kind: "success",
        title: "Playlist deleted",
        description: name,
      });
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
      <Button variant="outline" onClick={() => setOpen(true)}>
        <Trash2 className="h-4 w-4" /> Delete
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete "{name}"?</DialogTitle>
            <DialogDescription>
              This removes the playlist from your Tidal account. It can't be
              undone.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={confirmDelete}
              disabled={submitting}
            >
              {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
              Delete
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
