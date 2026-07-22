import { useState } from "react";
import { Check, Layers, Loader2, Plus } from "lucide-react";
import { api } from "@/api/client";
import type { Album, AlbumCollectionSummary } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/toast";

/**
 * Adds an album to a local collection (#243). A dropdown lists the
 * user's collections and offers "New collection"; both file this album
 * away. Collections are on-device only, so this never touches Tidal.
 *
 * We send a trimmed album payload rather than the whole AlbumDetail
 * (which carries tracks, similar albums, etc.) — the store only keeps
 * the fields needed to render a card and reopen the album.
 */
export function AddToCollectionButton({ album }: { album: Album }) {
  const toast = useToast();
  const [collections, setCollections] = useState<
    AlbumCollectionSummary[] | null
  >(null);
  const [createOpen, setCreateOpen] = useState(false);

  const payload: Album = {
    kind: "album",
    id: album.id,
    name: album.name,
    cover: album.cover,
    artists: album.artists,
    year: album.year,
    num_tracks: album.num_tracks,
    duration: album.duration,
    explicit: album.explicit,
    available: album.available,
    album_type: album.album_type,
  };

  const loadOnOpen = (open: boolean) => {
    if (!open) return;
    api.collections
      .list()
      .then(setCollections)
      .catch(() => setCollections([]));
  };

  const addTo = async (c: AlbumCollectionSummary) => {
    try {
      const res = await api.collections.addAlbum(c.id, payload);
      toast.show({
        kind: "success",
        title: res.added ? `Added to "${c.name}"` : `Already in "${c.name}"`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't add to collection",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const createAndAdd = async (name: string) => {
    try {
      const c = await api.collections.create(name);
      await api.collections.addAlbum(c.id, payload);
      setCreateOpen(false);
      toast.show({ kind: "success", title: `Added to "${name}"` });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't create collection",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <>
      <DropdownMenu onOpenChange={loadOnOpen}>
        <DropdownMenuTrigger asChild>
          {/* Same bare icon-over-caption pattern as the sibling row
              controls (AddToLibraryButton / ShareButton), not the
              shadcn <Button> — that rendered a smaller icon (the button
              base class forces svg to size-4 over our h-5), no caption,
              a stray hover pill, and the wrong height. */}
          <button
            className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground data-[state=open]:text-primary"
            aria-label="Add to collection"
            title="Save this album to one of your own on-device collections — personal groups of albums that Tidal can't do (like folders or tags), and that work offline"
          >
            <Layers className="h-5 w-5" />
            <div className="text-xs font-semibold">Collection</div>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
          <DropdownMenuLabel>Add to collection</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {collections === null && (
            <DropdownMenuItem disabled>
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </DropdownMenuItem>
          )}
          {collections?.map((c) => (
            <DropdownMenuItem key={c.id} onSelect={() => addTo(c)}>
              <Check className="h-4 w-4 opacity-0" />
              <span className="truncate">{c.name}</span>
            </DropdownMenuItem>
          ))}
          {collections !== null && (
            <>
              {collections.length > 0 && <DropdownMenuSeparator />}
              <DropdownMenuItem onSelect={() => setCreateOpen(true)}>
                <Plus className="h-4 w-4" /> New collection…
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {createOpen && (
        <CreateDialog
          onCancel={() => setCreateOpen(false)}
          onSubmit={createAndAdd}
        />
      )}
    </>
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
            <Label htmlFor="new-collection-name">Name</Label>
            <Input
              id="new-collection-name"
              autoFocus
              placeholder="e.g. Chill, Vinyl to buy"
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
              Create &amp; add
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
