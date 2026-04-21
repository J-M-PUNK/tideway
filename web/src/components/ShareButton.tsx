import { Share2 } from "lucide-react";
import { useToast } from "@/components/toast";

interface Props {
  shareUrl: string | null | undefined;
}

/**
 * "Share" button for a detail-page actions row. Icon above, "Share"
 * label below — matches the other labeled icon buttons in the cluster.
 * No-ops when there's no share URL (still renders so the layout
 * doesn't shift as the page hydrates).
 */
export function ShareButton({ shareUrl }: Props) {
  const toast = useToast();
  const disabled = !shareUrl;

  const onClick = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      toast.show({ kind: "success", title: "Link copied", description: shareUrl });
    } catch {
      toast.show({
        kind: "error",
        title: "Copy failed",
        description: "Clipboard not available.",
      });
    }
  };

  return (
    <button
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onClick();
      }}
      disabled={disabled}
      className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
      title="Copy link"
      aria-label="Copy link"
    >
      <Share2 className="h-5 w-5" />
      <div className="text-xs font-semibold">Share</div>
    </button>
  );
}
