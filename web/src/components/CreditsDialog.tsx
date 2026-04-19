import { Link } from "react-router-dom";
import { Info, Loader2 } from "lucide-react";
import { useCredits } from "@/hooks/useCredits";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * Controlled dialog that renders grouped contributor rows. The credits
 * fetch is deferred to useCredits, which caches by trackId — opening and
 * closing for the same track is instant on the second open.
 */
export function CreditsDialog({
  trackId,
  trackName,
  open,
  onOpenChange,
}: {
  trackId: string;
  trackName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  // Only actually hit the network when the dialog is open.
  const { credits, loading } = useCredits(trackId, open);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Credits</DialogTitle>
          <DialogDescription className="truncate">{trackName}</DialogDescription>
        </DialogHeader>

        {loading && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading credits…
          </div>
        )}
        {!loading && credits && credits.length === 0 && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Info className="h-4 w-4" /> No credits listed for this track.
          </div>
        )}
        {credits && credits.length > 0 && (
          <div className="flex flex-col gap-4">
            {credits.map((entry) => (
              <div key={entry.role} className="flex flex-col gap-1">
                <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {entry.role}
                </div>
                <div className="text-sm">
                  {entry.contributors.map((c, i) => (
                    <span key={`${c.name}-${i}`}>
                      {i > 0 && <span className="text-muted-foreground">, </span>}
                      {c.id ? (
                        <Link
                          to={`/artist/${c.id}`}
                          onClick={() => onOpenChange(false)}
                          className="hover:underline"
                        >
                          {c.name}
                        </Link>
                      ) : (
                        c.name
                      )}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
