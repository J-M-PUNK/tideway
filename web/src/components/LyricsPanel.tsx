import { useEffect, useMemo, useRef } from "react";
import { Loader2, Mic2, Music, X } from "lucide-react";
import { useLyrics } from "@/hooks/useLyrics";
import { usePlayerActions, usePlayerMeta, usePlayerTime } from "@/hooks/PlayerContext";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import { cn, imageProxy } from "@/lib/utils";

/**
 * Right-side lyrics panel. Fetches lyrics when the current track changes;
 * if synced lyrics are available the currently-playing line highlights and
 * auto-scrolls to center.
 */
export function LyricsPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { track } = usePlayerMeta();
  const { currentTime } = usePlayerTime();
  const actions = usePlayerActions();
  const { lyrics, loading } = useLyrics(open ? track?.id ?? null : null);

  const activeIdx = useMemo(() => {
    if (!lyrics?.synced) return -1;
    // Last line whose start ≤ currentTime.
    let idx = -1;
    for (let i = 0; i < lyrics.synced.length; i++) {
      if (lyrics.synced[i].time <= currentTime) idx = i;
      else break;
    }
    return idx;
  }, [lyrics, currentTime]);

  return (
    <>
      <div
        className={cn(
          "fixed inset-0 z-40 bg-black/40 backdrop-blur-sm transition-opacity",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={onClose}
      />
      <aside
        className={cn(
          "fixed right-0 top-0 z-50 flex h-full w-[28rem] flex-col border-l border-border bg-card shadow-2xl transition-transform",
          open ? "translate-x-0" : "translate-x-full",
        )}
        aria-hidden={!open}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <Mic2 className="h-4 w-4" />
            <h2 className="text-sm font-semibold">Lyrics</h2>
          </div>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>

        {track && (
          <div className="flex items-center gap-3 border-b border-border px-4 py-3">
            <div className="h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary">
              {track.album?.cover ? (
                <img
                  src={imageProxy(track.album.cover)}
                  alt=""
                  className="h-full w-full object-cover"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center text-muted-foreground">
                  <Music className="h-4 w-4" />
                </div>
              )}
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">{track.name}</div>
              <div className="truncate text-xs text-muted-foreground">
                {track.artists.map((a) => a.name).join(", ")}
              </div>
            </div>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin px-6 py-8">
          {!track ? (
            <EmptyState icon={Mic2} title="Play a track" description="Lyrics appear when a track is playing." />
          ) : loading ? (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : lyrics?.synced && lyrics.synced.length > 0 ? (
            <SyncedLyrics lines={lyrics.synced} active={activeIdx} onSeek={actions.seek} />
          ) : lyrics?.text ? (
            <pre className="whitespace-pre-wrap font-sans text-base leading-relaxed text-foreground">
              {lyrics.text}
            </pre>
          ) : (
            <EmptyState
              icon={Mic2}
              title="No lyrics available"
              description="Tidal doesn't have lyrics for this track."
            />
          )}
        </div>
      </aside>
    </>
  );
}

function SyncedLyrics({
  lines,
  active,
  onSeek,
}: {
  lines: { time: number; text: string }[];
  active: number;
  onSeek: (t: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const activeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    activeRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [active]);

  return (
    <div ref={containerRef} className="flex flex-col gap-3">
      {lines.map((line, i) => {
        const isActive = i === active;
        return (
          <button
            ref={isActive ? activeRef : null}
            key={i}
            onClick={() => onSeek(line.time)}
            className={cn(
              "cursor-pointer rounded px-2 py-1 text-left text-lg font-semibold leading-snug transition-all",
              isActive
                ? "text-primary"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {line.text}
          </button>
        );
      })}
    </div>
  );
}
