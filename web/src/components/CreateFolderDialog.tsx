import { useState } from "react";
import { FolderPlus, Loader2 } from "lucide-react";
import { api } from "@/api/client";
import type { PlaylistFolder } from "@/api/types";
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

export function CreateFolderDialog({
  trigger,
  onCreated,
  parentId = "root",
}: {
  trigger?: React.ReactNode;
  onCreated?: (folder: PlaylistFolder) => void;
  parentId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const toast = useToast();

  const submit = async () => {
    if (!name.trim() || submitting) return;
    setSubmitting(true);
    try {
      const folder = await api.library.folders.create(name.trim(), parentId);
      toast.show({
        kind: "success",
        title: "Folder created",
        description: folder.name,
      });
      setOpen(false);
      setName("");
      onCreated?.(folder);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't create folder",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="secondary">
            <FolderPlus className="h-4 w-4" /> New folder
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create folder</DialogTitle>
          <DialogDescription>
            Group playlists together. Folders sync to your Tidal account and
            show up on mobile too.
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
            <Label htmlFor="folder-name">Name</Label>
            <Input
              id="folder-name"
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My playlists"
              maxLength={200}
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
            <Button type="submit" disabled={!name.trim() || submitting}>
              {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
              Create
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
