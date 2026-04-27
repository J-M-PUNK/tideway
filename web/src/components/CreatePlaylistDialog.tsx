import { useState } from "react";
import { Loader2, Plus } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/toast";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";

export function CreatePlaylistDialog({
  trigger,
  onCreated,
  open: controlledOpen,
  onOpenChange,
}: {
  trigger?: React.ReactNode;
  onCreated?: (playlistId: string) => void;
  /** Pass to control the dialog from a parent (e.g. command palette). */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlledOpen ?? internalOpen;
  const setOpen = (value: boolean) => {
    if (onOpenChange) onOpenChange(value);
    else setInternalOpen(value);
  };
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const toast = useToast();
  const { optimisticAdd, refresh } = useMyPlaylists();

  const submit = async () => {
    if (!title.trim() || submitting) return;
    setSubmitting(true);
    try {
      const p = await api.playlists.create(title.trim(), description.trim());
      optimisticAdd(p);
      refresh().catch(() => {});
      toast.show({
        kind: "success",
        title: "Playlist created",
        description: p.name,
      });
      setOpen(false);
      setTitle("");
      setDescription("");
      onCreated?.(p.id);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't create playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  };

  // Render the default trigger only when we're uncontrolled. Consumers who
  // want their own UI pass `trigger`; the command palette passes neither and
  // drives the dialog via `open`/`onOpenChange`.
  const showTrigger = trigger !== undefined || controlledOpen === undefined;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {showTrigger && (
        <DialogTrigger asChild>
          {trigger ?? (
            <Button size="sm" variant="secondary">
              <Plus className="h-4 w-4" /> New playlist
            </Button>
          )}
        </DialogTrigger>
      )}
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create playlist</DialogTitle>
          <DialogDescription>
            Give it a name. You can add tracks afterwards.
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          className="flex flex-col gap-4"
        >
          <div className="flex flex-col gap-2">
            <Label htmlFor="playlist-title">Name</Label>
            <Input
              id="playlist-title"
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="My new playlist"
              maxLength={200}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="playlist-desc">Description</Label>
            <Input
              id="playlist-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional"
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
              Create
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
