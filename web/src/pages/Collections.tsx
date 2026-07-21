import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FolderPlus, Layers, Loader2, Music, Plus } from "lucide-react";
import { api } from "@/api/client";
import type { AlbumCollectionSummary } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { EmptyState } from "@/components/EmptyState";
import { ErrorView } from "@/components/ErrorView";
import { Grid } from "@/components/Grid";
import { GridSkeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import { imageProxy } from "@/lib/utils";

/**
 * Local album collections (#243) — user-defined groups of favorite
 * albums that live only on this device (Tidal has no album-folder
 * API). Lists the collections as folder-style cards; creating one and
 * opening it happen here, adding albums happens from an album's page.
 */
export function Collections() {
  const toast = useToast();
  const [collections, setCollections] = useState<
    AlbumCollectionSummary[] | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const load = async () => {
    try {
      setCollections(await api.collections.list());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    let cancelled = false;
    api.collections
      .list()
      .then((c) => {
        if (!cancelled) setCollections(c);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const onCreate = async (name: string) => {
    try {
      await api.collections.create(name);
      setCreateOpen(false);
      toast.show({ kind: "success", title: `Created "${name}"` });
      await load();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't create collection",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  if (error) return <ErrorView error={error} />;

  return (
    <div>
      <div className="mb-6 flex items-center justify-between gap-3">
        <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
          <Layers className="h-7 w-7" /> Collections
        </h1>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" /> New collection
        </Button>
      </div>

      {!collections && <GridSkeleton />}

      {collections && collections.length === 0 && (
        <EmptyState
          icon={FolderPlus}
          title="No collections yet"
          description="Create a collection, then open any album and use “Add to collection” to file it here. Collections live on this device only."
        />
      )}

      {collections && collections.length > 0 && (
        <Grid>
          {collections.map((c) => (
            <CollectionCard key={c.id} collection={c} />
          ))}
        </Grid>
      )}

      {createOpen && (
        <CreateDialog
          onCancel={() => setCreateOpen(false)}
          onSubmit={onCreate}
        />
      )}
    </div>
  );
}

function CollectionCard({
  collection,
}: {
  collection: AlbumCollectionSummary;
}) {
  return (
    <Link
      to={`/library/collection/${encodeURIComponent(collection.id)}`}
      className="group flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors duration-200 ease-out hover:bg-accent"
    >
      <CollectionThumb covers={collection.covers} />
      <div className="min-w-0">
        <div className="truncate font-semibold">{collection.name}</div>
        <div className="mt-0.5 text-xs text-muted-foreground">
          {collection.count} {collection.count === 1 ? "album" : "albums"}
        </div>
      </div>
    </Link>
  );
}

/** 2x2 mosaic of the first four covers, mirroring how desktop file
 *  managers render a folder preview. Falls back to a single icon tile
 *  when the collection is empty. */
function CollectionThumb({ covers }: { covers: string[] }) {
  if (covers.length === 0) {
    return (
      <div className="flex aspect-square w-full items-center justify-center rounded-md bg-secondary text-muted-foreground">
        <Music className="h-10 w-10" />
      </div>
    );
  }
  // Pad to 4 tiles so the grid stays square even with 1–3 covers.
  const tiles = [...covers.slice(0, 4)];
  while (tiles.length < 4) tiles.push("");
  return (
    <div className="grid aspect-square w-full grid-cols-2 grid-rows-2 gap-0.5 overflow-hidden rounded-md bg-secondary">
      {tiles.map((cover, i) => (
        <div key={i} className="overflow-hidden bg-secondary">
          {cover ? (
            <img
              src={imageProxy(cover)}
              alt=""
              loading="lazy"
              className="h-full w-full object-cover"
            />
          ) : null}
        </div>
      ))}
    </div>
  );
}

function CreateDialog({
  onCancel,
  onSubmit,
}: {
  onCancel: () => void;
  onSubmit: (name: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
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
          <DialogTitle>New collection</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          className="flex flex-col gap-4"
        >
          <div className="flex flex-col gap-2">
            <Label htmlFor="collection-name">Name</Label>
            <Input
              id="collection-name"
              autoFocus
              placeholder="e.g. Chill, Vinyl to buy, Best of 2024"
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
              Create
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
