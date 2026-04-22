import { useEffect, useState } from "react";
import { Link2, Loader2, Plus } from "lucide-react";
import { api } from "@/api/client";
import type { QualityOption } from "@/api/types";
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

export function AddUrlDialog({ trigger }: { trigger?: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [quality, setQuality] = useState<string>("");
  const [qualities, setQualities] = useState<QualityOption[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    if (!open) return;
    if (qualities.length) return;
    let cancelled = false;
    (async () => {
      try {
        const qs = await api.qualities();
        if (cancelled) return;
        setQualities(qs);
      } catch {
        // Fetch failure shouldn't break the dialog — the user can still
        // paste a URL and leave "Highest available" selected.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, qualities]);

  const submit = async () => {
    if (!url.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.downloads.enqueueUrl(url.trim(), quality || undefined);
      setUrl("");
      setOpen(false);
      toast.show({ kind: "success", title: "Queued", description: "Added to downloads." });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button variant="secondary" size="sm">
            <Plus className="h-4 w-4" /> Paste URL
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Download from Tidal URL</DialogTitle>
          <DialogDescription>
            Paste any tidal.com link — track, album, or playlist — to enqueue it.
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
            <Label htmlFor="tidal-url">Tidal URL</Label>
            <div className="relative">
              <Link2 className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                id="tidal-url"
                autoFocus
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://tidal.com/browse/album/123456789"
                className="pl-10"
              />
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="quality">Quality</Label>
            <select
              id="quality"
              value={quality}
              onChange={(e) => setQuality(e.target.value)}
              className="h-10 rounded-md border border-input bg-secondary px-3 text-sm"
            >
              <option value="">Highest available</option>
              {qualities.map((q) => (
                <option key={q.value} value={q.value}>
                  {q.label} — {q.codec} · {q.bitrate}
                </option>
              ))}
            </select>
          </div>

          {error && <p className="text-xs text-destructive">{error}</p>}

          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={submitting || !url.trim()}>
              {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              Download
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
