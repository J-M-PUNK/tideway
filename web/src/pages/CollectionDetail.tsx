import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ChevronLeft,
  Layers,
  Loader2,
  Music,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import { api } from "@/api/client";
import type { Album, AlbumCollectionDetail } from "@/api/types";
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
import { PlayMediaButton } from "@/components/PlayMediaButton";
import { useToast } from "@/components/toast";
import { cn, imageProxy } from "@/lib/utils";

/**
 * Detail view for a local album collection (#243). Shows the albums in
 * a grid with a per-card remove button, plus rename / delete. Adding
 * albums happens from an album's own page ("Add to collection").
 */
export function CollectionDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();

  const [collection, setCollection] = useState<AlbumCollectionDetail | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.collections
      .get(id)
      .then((c) => {
        if (!cancelled) setCollection(c);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const onRename = async (name: string) => {
    try {
      await api.collections.rename(id, name);
      setCollection((c) => (c ? { ...c, name } : c));
      setRenameOpen(false);
      toast.show({ kind: "success", title: "Collection renamed" });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't rename collection",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const onDelete = async () => {
    try {
      await api.collections.delete(id);
      toast.show({ kind: "success", title: "Collection deleted" });
      navigate("/library/collections");
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't delete collection",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const onRemove = async (album: Album) => {
    // Optimistic: drop it from the grid immediately, roll back on error.
    const prev = collection;
    setCollection((c) =>
      c ? { ...c, albums: c.albums.filter((a) => a.id !== album.id) } : c,
    );
    try {
      await api.collections.removeAlbum(id, album.id);
    } catch (err) {
      setCollection(prev);
      toast.show({
        kind: "error",
        title: "Couldn't remove album",
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
            <Link to="/library/collections">
              <ChevronLeft className="h-4 w-4" /> Collections
            </Link>
          </Button>
          <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
            <Layers className="h-7 w-7" /> {collection?.name ?? "Collection"}
          </h1>
        </div>
        {collection && (
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
        )}
      </div>

      {!collection && <GridSkeleton />}

      {collection && collection.albums.length === 0 && (
        <EmptyState
          icon={Music}
          title="No albums yet"
          description="Open any album and use “Add to collection” to file it here."
        />
      )}

      {collection && collection.albums.length > 0 && (
        <Grid>
          {collection.albums.map((album) => (
            <CollectionAlbumCard
              key={album.id}
              album={album}
              onRemove={() => onRemove(album)}
            />
          ))}
        </Grid>
      )}

      {renameOpen && collection && (
        <RenameDialog
          initial={collection.name}
          onCancel={() => setRenameOpen(false)}
          onSubmit={onRename}
        />
      )}
      {deleteOpen && collection && (
        <DeleteDialog
          name={collection.name}
          onCancel={() => setDeleteOpen(false)}
          onConfirm={onDelete}
        />
      )}
    </div>
  );
}

function CollectionAlbumCard({
  album,
  onRemove,
}: {
  album: Album;
  onRemove: () => void;
}) {
  const cover = imageProxy(album.cover);
  const subtitle = (album.artists ?? []).map((a) => a.name).join(", ");
  return (
    <div className="group relative flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors duration-200 ease-out hover:bg-accent">
      <Link
        to={`/album/${album.id}`}
        className="relative aspect-square w-full overflow-hidden rounded-md bg-secondary"
      >
        {cover ? (
          <img
            src={cover}
            alt={album.name}
            loading="lazy"
            className="h-full w-full object-cover transition-transform duration-300 ease-out group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-10 w-10" />
          </div>
        )}
        <div className="absolute bottom-2 left-2 opacity-0 transition-all duration-200 ease-out group-hover:opacity-100 focus-within:opacity-100">
          <PlayMediaButton kind="album" id={album.id} className="h-10 w-10" />
        </div>
      </Link>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${album.name} from collection`}
        title="Remove from collection"
        className={cn(
          "absolute right-2 top-2 flex h-8 w-8 items-center justify-center rounded-full bg-black/70 text-white opacity-0 shadow-lg transition-all hover:bg-black/90 group-hover:opacity-100 focus-visible:opacity-100",
        )}
      >
        <X className="h-4 w-4" />
      </button>
      <div className="min-w-0">
        <Link to={`/album/${album.id}`} className="block">
          <div className="truncate font-semibold">{album.name}</div>
        </Link>
        {subtitle && (
          <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
            {subtitle}
          </div>
        )}
      </div>
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
          <DialogTitle>Rename collection</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          className="flex flex-col gap-4"
        >
          <div className="flex flex-col gap-2">
            <Label htmlFor="collection-rename">Name</Label>
            <Input
              id="collection-rename"
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
            This removes the collection from this device. The albums themselves
            and your Tidal favorites are untouched.
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
