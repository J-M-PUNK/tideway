import { useEffect, useState } from "react";
import { Link2 } from "lucide-react";
import { api } from "@/api/client";
import { useToast } from "@/components/toast";
import { cn } from "@/lib/utils";

// Accepts:
//   https://tidal.com/browse/track/1234
//   https://www.tidal.com/...
//   https://listen.tidal.com/...
//   https://link.tidal.com/abcd (share links)
const TIDAL_URL_RE =
  /https?:\/\/(?:www\.|listen\.|link\.)?tidal\.com\/[^\s]+/i;

/**
 * Listens at the window level for drag-and-drop of Tidal URLs. The user
 * can drag a link from their browser's address bar, a search result, or a
 * shared message, drop it anywhere in the app, and it's queued for
 * download.
 *
 * Shows a full-screen translucent hint while a drag is active over the
 * window so the user knows drop is supported.
 */
export function UrlDropTarget() {
  const [active, setActive] = useState(false);
  const toast = useToast();

  useEffect(() => {
    // Depth counter: dragenter/dragleave fire per nested element. Tracking
    // a counter prevents the overlay from flickering as the pointer moves
    // between child elements.
    let depth = 0;

    const isDialogOpen = () =>
      // Radix Dialog + ContextMenu + DropdownMenu all mark their portaled
      // roots with data-state="open". If any of those are up, the user is
      // mid-interaction elsewhere — don't hijack their drag.
      document.querySelector('[role="dialog"][data-state="open"]') !== null ||
      document.querySelector('[role="menu"][data-state="open"]') !== null;

    const shouldAccept = (e: DragEvent) => {
      if (!e.dataTransfer) return false;
      if (isDialogOpen()) return false;
      const types = Array.from(e.dataTransfer.types);
      // Browsers mark URL-shaped drags with "text/uri-list" or "text/plain".
      return types.includes("text/uri-list") || types.includes("text/plain");
    };

    const onDragEnter = (e: DragEvent) => {
      if (!shouldAccept(e)) return;
      e.preventDefault();
      depth += 1;
      setActive(true);
    };
    const onDragOver = (e: DragEvent) => {
      if (!shouldAccept(e)) return;
      // preventDefault on dragover is required for the drop event to fire.
      e.preventDefault();
    };
    const onDragLeave = (e: DragEvent) => {
      if (!shouldAccept(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setActive(false);
    };
    const onDrop = async (e: DragEvent) => {
      depth = 0;
      setActive(false);
      if (!e.dataTransfer) return;
      e.preventDefault();
      const raw =
        e.dataTransfer.getData("text/uri-list") ||
        e.dataTransfer.getData("text/plain") ||
        "";
      const match = raw.match(TIDAL_URL_RE);
      if (!match) {
        toast.show({
          kind: "error",
          title: "Not a Tidal URL",
          description: "Drop a link from tidal.com to download it.",
        });
        return;
      }
      try {
        await api.downloads.enqueueUrl(match[0]);
        toast.show({
          kind: "success",
          title: "Added to downloads",
          description: match[0],
        });
      } catch (err) {
        toast.show({
          kind: "error",
          title: "Couldn't enqueue",
          description: err instanceof Error ? err.message : String(err),
        });
      }
    };

    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [toast]);

  return (
    <div
      className={cn(
        "pointer-events-none fixed inset-0 z-[90] flex items-center justify-center bg-primary/20 backdrop-blur-sm transition-opacity",
        active ? "opacity-100" : "opacity-0",
      )}
      aria-hidden={!active}
    >
      <div className="flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed border-primary bg-black/60 px-10 py-8 text-foreground">
        <Link2 className="h-8 w-8 text-primary" />
        <div className="text-lg font-semibold">Drop Tidal URL to download</div>
        <div className="text-xs text-muted-foreground">
          Track, album, or playlist URLs work.
        </div>
      </div>
    </div>
  );
}
