import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronLeft, Folder, Loader2, Pencil, Trash2 } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import type { Playlist, PlaylistFolder } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { Grid } from "@/components/Grid";
import { GridSkeleton } from "@/components/Skeletons";
import { MediaCard } from "@/components/MediaCard";
import { useToast } from "@/components/toast";

/**
 * Detail view for a playlist folder. Shows the folder's playlists in a
 * grid, plus rename / delete actions. Creating folders happens on the
 * library page; moving playlists in/out happens from the playlist
 * detail page.
 */
export function FolderDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();

  const [folder, setFolder] = useState<PlaylistFolder | null>(null);
  const [playlists, setPlaylists] = useState<Playlist[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // Folder metadata isn't exposed as its own endpoint — we
        // grab the root folder list and filter. For nested folders
        // we'd need to walk; root-level is the common case.
        const folders = await api.library.folders.list("root");
        if (cancelled) return;
        const match = folders.find((f) => f.id === id) ?? null;
        setFolder(match);
        const pls = await api.library.folders.playlists(id);
        if (!cancelled) setPlaylists(pls);
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  const onRename = async (newName: string) => {
    try {
      await api.library.folders.rename(id, newName);
      setFolder((f) => (f ? { ...f, name: newName } : f));
      setRenameOpen(false);
      toast.show({ kind: "success", title: "Folder renamed" });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't rename folder",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const onDelete = async () => {
    try {
      await api.library.folders.delete(id);
      toast.show({ kind: "success", title: "Folder deleted" });
      navigate("/library/playlists");
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't delete folder",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  if (error) return <ErrorView error={error} />;

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button asChild variant="ghost" size="sm">
            <Link to="/library/playlists">
              <ChevronLeft className="h-4 w-4" /> Playlists
            </Link>
          </Button>
          <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
            <Folder className="h-7 w-7" /> {folder?.name ?? "Folder"}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setRenameOpen(true)}
          >
            <Pencil className="h-4 w-4" /> Rename
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setDeleteOpen(true)}
            className="text-destructive hover:text-destructive"
          >
            <Trash2 className="h-4 w-4" /> Delete
          </Button>
        </div>
      </div>

      {!playlists && <GridSkeleton />}

      {playlists && playlists.length === 0 && (
        <EmptyState
          icon={Folder}
          title="Empty folder"
          description="Open any playlist and use Move to folder to drop it in here."
        />
      )}

      {playlists && playlists.length > 0 && (
        <Grid>
          {playlists.map((p) => (
            <MediaCard key={p.id} item={p} onDownload={onDownload} />
          ))}
        </Grid>
      )}

      {renameOpen && folder && (
        <RenameDialog
          initial={folder.name}
          onCancel={() => setRenameOpen(false)}
          onSubmit={onRename}
        />
      )}
      {deleteOpen && folder && (
        <DeleteDialog
          name={folder.name}
          onCancel={() => setDeleteOpen(false)}
          onConfirm={onDelete}
        />
      )}
    </div>
  );
}

function RenameDialog({
  initial,
  onCancel,
  onSubmit,
}: {
  initial: string;
  onCancel: () => void;
  onSubmit: (name: string) => Promise<void>;
}) {
  const [name, setName] = useState(initial);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      await onSubmit(name.trim());
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename folder</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          className="flex flex-col gap-4"
        >
          <div className="flex flex-col gap-2">
            <Label htmlFor="rename-input">Name</Label>
            <Input
              id="rename-input"
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onCancel}>
              Cancel
            </Button>
            <Button type="submit" disabled={!name.trim() || busy}>
              {busy && <Loader2 className="h-4 w-4 animate-spin" />}
              Save
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DeleteDialog({
  name,
  onCancel,
  onConfirm,
}: {
  name: string;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const confirm = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete "{name}"?</DialogTitle>
          <DialogDescription>
            The folder is removed from your Tidal account. Any playlists inside
            move back to the top level — they aren't deleted.
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={confirm} disabled={busy}>
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            Delete
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
