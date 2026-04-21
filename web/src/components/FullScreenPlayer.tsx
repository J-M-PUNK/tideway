import { useEffect, useMemo, useRef } from "react";
import { Link } from "react-router-dom";
import {
  ChevronDown,
  Loader2,
  Mic2,
  Minimize2,
  Music,
  Pause,
  Play,
  Repeat,
  Repeat1,
  Shuffle,
  SkipBack,
  SkipForward,
} from "lucide-react";
import type { OnDownload } from "@/api/download";
import type { Lyrics } from "@/api/types";
import { useCoverColor } from "@/hooks/useCoverColor";
import { useIsDownloaded } from "@/hooks/useDownloadedSet";
import { useLyrics } from "@/hooks/useLyrics";
import { usePlayerActions, usePlayerMeta, usePlayerTime } from "@/hooks/PlayerContext";
import { Button } from "@/components/ui/button";
import { HeartButton } from "@/components/HeartButton";
import { DownloadButton } from "@/components/DownloadButton";
import { cn, formatDuration, imageProxy } from "@/lib/utils";

/**
 * Full-screen Now Playing view. Big cover on the left, synced lyrics (if
 * available) on the right, controls below. Background fades from the cover's
 * dominant color.
 */
export function FullScreenPlayer({
  open,
  onClose,
  onDownload,
}: {
  open: boolean;
  onClose: () => void;
  onDownload: OnDownload;
}) {
  const { track, playing, loading, shuffle, repeat, hasNext, hasPrev } = usePlayerMeta();
  const { currentTime, duration } = usePlayerTime();
  const actions = usePlayerActions();

  useEffect(() => {
    if (!open) return;
    // Capture phase + stopImmediatePropagation so Esc closes the
    // full-screen player and nothing else — otherwise AppInner's
    // shared Esc handler would ALSO close any open lyrics/queue panel
    // underneath, which the user can't see and didn't ask to close.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, onClose]);

  const isLocal = useIsDownloaded(track?.id ?? "");
  const cover = imageProxy(track?.album?.cover);
  const dominant = useCoverColor(cover);
  const pct = duration > 0 ? (currentTime / duration) * 100 : 0;

  // Shared with LyricsPanel — same hook, same cache, so opening one view
  // doesn't refetch what the other already has.
  const { lyrics, loading: loadingLyrics } = useLyrics(open ? track?.id ?? null : null);
  const activeIdx = useActiveLyric(lyrics, currentTime);

  if (!open || !track) return null;

  const bg = dominant
    ? `linear-gradient(180deg, ${dominant} 0%, ${dominant}aa 40%, hsl(0 0% 5%) 85%)`
    : "linear-gradient(180deg, hsl(0 0% 20%), hsl(0 0% 5%))";

  return (
    <div
      className="fixed inset-0 z-[70] flex flex-col text-foreground transition-opacity"
      style={{ background: bg }}
      role="dialog"
      aria-modal="true"
    >
      <div className="flex items-center justify-between px-6 py-4">
        <div className="min-w-0">
          <div className="text-xs uppercase tracking-wider text-foreground/70">
            {isLocal ? "Playing from your library" : "Playing preview"}
          </div>
          {track.album && (
            <Link
              to={`/album/${track.album.id}`}
              onClick={onClose}
              className="truncate text-sm font-semibold hover:underline"
            >
              {track.album.name}
            </Link>
          )}
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={onClose}
          className="h-9 w-9 bg-black/20 hover:bg-black/40"
          aria-label="Collapse"
        >
          <ChevronDown className="h-5 w-5" />
        </Button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-8 px-6 pb-6 md:flex-row md:items-center md:px-12">
        <div className="flex flex-1 items-center justify-center">
          <div className="aspect-square w-full max-w-[min(60vh,500px)] overflow-hidden rounded-lg bg-black/40 shadow-2xl">
            {cover ? (
              <img src={cover} alt="" className="h-full w-full object-cover" />
            ) : (
              <div className="flex h-full w-full items-center justify-center">
                <Music className="h-24 w-24 text-foreground/40" />
              </div>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col justify-center">
          {lyrics && lyrics.synced && lyrics.synced.length > 0 ? (
            <SyncedLyricsPane
              lines={lyrics.synced}
              active={activeIdx}
              onSeek={actions.seek}
            />
          ) : lyrics?.text ? (
            <pre className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap font-sans text-lg leading-relaxed text-foreground/80 scrollbar-thin">
              {lyrics.text}
            </pre>
          ) : loadingLyrics ? (
            <div className="flex items-center gap-2 text-foreground/60">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading lyrics…
            </div>
          ) : (
            <LyricsPlaceholder />
          )}
        </div>
      </div>

      {/* Footer controls */}
      <div className="border-t border-white/10 bg-black/30 px-6 py-4 backdrop-blur-sm">
        <div className="flex items-center gap-4">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <div className="min-w-0">
              <div className="truncate text-base font-semibold">{track.name}</div>
              <div className="truncate text-sm text-foreground/70">
                {track.artists.map((a, i) => (
                  <span key={a.id}>
                    {i > 0 && ", "}
                    <Link
                      to={`/artist/${a.id}`}
                      onClick={onClose}
                      className="hover:underline"
                    >
                      {a.name}
                    </Link>
                  </span>
                ))}
              </div>
            </div>
            <HeartButton kind="track" id={track.id} size="sm" />
            <DownloadButton
              kind="track"
              id={track.id}
              onPick={onDownload}
              iconOnly
              variant="ghost"
              audioModes={track.audio_modes}
              mediaTags={track.media_tags}
            />
          </div>

          <div className="flex flex-1 flex-col items-center gap-2">
            <div className="flex items-center gap-3">
              <Button
                variant="ghost"
                size="icon"
                onClick={actions.toggleShuffle}
                className={cn("h-9 w-9 hover:bg-white/10", shuffle && "text-primary")}
                title="Shuffle"
              >
                <Shuffle className="h-4 w-4" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                onClick={actions.prev}
                disabled={!hasPrev && currentTime < 3}
                className="h-10 w-10 hover:bg-white/10"
              >
                <SkipBack className="h-5 w-5" fill="currentColor" />
              </Button>
              <Button
                size="icon"
                onClick={actions.toggle}
                className="h-12 w-12 rounded-full"
                disabled={loading && !playing}
              >
                {loading ? (
                  <Loader2 className="h-5 w-5 animate-spin" />
                ) : playing ? (
                  <Pause className="h-5 w-5" fill="currentColor" />
                ) : (
                  <Play className="h-5 w-5" fill="currentColor" />
                )}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                onClick={actions.next}
                disabled={!hasNext && !shuffle && repeat === "off"}
                className="h-10 w-10 hover:bg-white/10"
              >
                <SkipForward className="h-5 w-5" fill="currentColor" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                onClick={actions.cycleRepeat}
                className={cn(
                  "h-9 w-9 hover:bg-white/10",
                  repeat !== "off" && "text-primary",
                )}
                title={
                  repeat === "off"
                    ? "Repeat off"
                    : repeat === "all"
                      ? "Repeat all"
                      : "Repeat one"
                }
              >
                {repeat === "one" ? (
                  <Repeat1 className="h-4 w-4" />
                ) : (
                  <Repeat className="h-4 w-4" />
                )}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                onClick={onClose}
                className="h-9 w-9 hover:bg-white/10"
                title="Minimize"
              >
                <Minimize2 className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex w-full max-w-xl items-center gap-2 text-[11px] text-foreground/70">
              <span className="w-10 text-right tabular-nums">{formatDuration(currentTime)}</span>
              <input
                type="range"
                min={0}
                max={Math.max(duration, 1)}
                step={0.1}
                value={Math.min(currentTime, duration || 0)}
                onChange={(e) => actions.seek(Number(e.target.value))}
                className="h-1 flex-1 cursor-pointer appearance-none rounded-full accent-primary"
                style={{
                  background: `linear-gradient(to right, white ${pct}%, rgba(255,255,255,0.2) ${pct}%)`,
                }}
                aria-label="Seek"
              />
              <span className="w-10 tabular-nums">{formatDuration(duration)}</span>
            </div>
          </div>

          <div className="flex flex-1 justify-end gap-2">
            <Button
              variant="ghost"
              size="icon"
              className="h-9 w-9 hover:bg-white/10"
              title="Lyrics toggle"
            >
              <Mic2 className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SyncedLyricsPane({
  lines,
  active,
  onSeek,
}: {
  lines: { time: number; text: string }[];
  active: number;
  onSeek: (t: number) => void;
}) {
  const activeRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    activeRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [active]);

  return (
    <div className="max-h-[55vh] overflow-y-auto scrollbar-thin">
      <div className="flex flex-col gap-2 py-12">
        {lines.map((line, i) => {
          const isActive = i === active;
          const delta = Math.abs(i - active);
          const opacity = isActive ? 1 : Math.max(0.25, 0.9 - delta * 0.15);
          return (
            <button
              ref={isActive ? activeRef : null}
              key={i}
              onClick={() => onSeek(line.time)}
              className={cn(
                "rounded px-3 py-1 text-left text-2xl font-bold leading-snug transition-all",
                isActive ? "text-foreground" : "text-foreground hover:text-foreground",
              )}
              style={{ opacity }}
            >
              {line.text}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function LyricsPlaceholder() {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-white/5 p-6 text-sm text-foreground/60">
      <Mic2 className="h-5 w-5 flex-shrink-0" />
      <span>No lyrics available for this track.</span>
    </div>
  );
}

function useActiveLyric(lyrics: Lyrics | null, currentTime: number): number {
  return useMemo(() => {
    if (!lyrics?.synced) return -1;
    let idx = -1;
    for (let i = 0; i < lyrics.synced.length; i++) {
      if (lyrics.synced[i].time <= currentTime) idx = i;
      else break;
    }
    return idx;
  }, [lyrics, currentTime]);
}
