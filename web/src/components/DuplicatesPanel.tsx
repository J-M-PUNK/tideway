import { useMemo, useState } from "react";
import { AlertTriangle, Loader2, Trash2 } from "lucide-react";
import type { Track } from "@/api/types";
import { api } from "@/api/client";
import { useToast } from "@/components/toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";

/**
 * Duplicate detection + remove-extras UI for an owned playlist.
 *
 * Keying: `(normalized_title, primary_artist_id, explicit)` — same
 * rule the artist-discography dedup uses server-side. That way the
 * explicit and clean cuts of the same song stay separate, and a cover
 * by a different artist stays separate, but a user who's added the
 * exact same track twice gets flagged.
 *
 * Remove-extras policy: keep the first occurrence of each group,
 * delete the rest. We delete by *descending index* so each delete
 * doesn't shift the indices we haven't processed yet. Fires the
 * existing `/api/playlists/{id}/tracks/{index}` endpoint sequentially
 * — the playlist mutation endpoints aren't safe to parallelize on
 * Tidal's side.
 */

export interface DuplicateGroup {
  key: string;
  /** Indices in the playlist's track list (matching what the server
   *  will accept for delete-by-index), sorted ascending. */
  indices: number[];
  /** Representative track — the first occurrence, shown in the UI. */
  track: Track;
}

export function computeDuplicateGroups(tracks: Track[]): DuplicateGroup[] {
  const groups = new Map<string, { track: Track; indices: number[] }>();
  for (let i = 0; i < tracks.length; i++) {
    const t = tracks[i];
    const name = (t.name || "").trim().toLowerCase();
    const primary = t.artists[0]?.id ?? "";
    const key = `${name}${primary}${t.explicit ? 1 : 0}`;
    const existing = groups.get(key);
    if (existing) {
      existing.indices.push(i);
    } else {
      groups.set(key, { track: t, indices: [i] });
    }
  }
  const out: DuplicateGroup[] = [];
  for (const [key, { track, indices }] of groups) {
    if (indices.length > 1) out.push({ key, track, indices });
  }
  return out;
}

export function DuplicatesPanel({
  open,
  onOpenChange,
  playlistId,
  groups,
  onRemoved,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  playlistId: string;
  groups: DuplicateGroup[];
  /** Fired after all extras are deleted server-side; parent should
   *  refresh its tracks. */
  onRemoved: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const totalExtras = useMemo(
    () => groups.reduce((s, g) => s + (g.indices.length - 1), 0),
    [groups],
  );

  const removeAllExtras = async () => {
    if (busy || totalExtras === 0) return;
    setBusy(true);
    // Collect every "extra" index across all groups (i.e. everything
    // past the first occurrence). Delete in descending order so the
    // remaining indices stay valid through the loop.
    const toDelete: number[] = [];
    for (const g of groups) {
      for (let i = 1; i < g.indices.length; i++) toDelete.push(g.indices[i]);
    }
    toDelete.sort((a, b) => b - a);
    try {
      for (const idx of toDelete) {
        await api.playlists.removeTrack(playlistId, idx);
      }
      toast.show({
        kind: "success",
        title: `Removed ${toDelete.length} duplicate ${toDelete.length === 1 ? "track" : "tracks"}`,
      });
      onOpenChange(false);
      onRemoved();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't remove duplicates",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => (!busy ? onOpenChange(v) : null)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-amber-400" />
            Duplicate tracks
          </DialogTitle>
          <DialogDescription>
            {totalExtras} duplicate{totalExtras === 1 ? "" : "s"} across{" "}
            {groups.length} track{groups.length === 1 ? "" : "s"}. Removing
            keeps the first occurrence of each.
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-80 overflow-y-auto">
          <ul className="flex flex-col divide-y divide-border/40">
            {groups.map((g) => (
              <li key={g.key} className="flex items-center gap-3 py-2 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{g.track.name}</div>
                  <div className="truncate text-xs text-muted-foreground">
                    {g.track.artists.map((a) => a.name).join(", ")}
                  </div>
                </div>
                <div className="flex-shrink-0 text-xs text-muted-foreground">
                  ×{g.indices.length}
                </div>
              </li>
            ))}
          </ul>
        </div>

        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button onClick={removeAllExtras} disabled={busy}>
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Trash2 className="h-4 w-4" />
            )}
            Remove extras
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
