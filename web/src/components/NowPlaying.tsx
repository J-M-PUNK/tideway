import { Link } from "react-router-dom";
import {
  ListMusic,
  Loader2,
  Mic2,
  Music,
  Pause,
  Play,
  Repeat,
  Repeat1,
  Shuffle,
  SkipBack,
  SkipForward,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import type { OnDownload } from "@/api/download";
import { formatDuration, imageProxy } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { DownloadButton } from "@/components/DownloadButton";
import { HeartButton } from "@/components/HeartButton";
import { SleepTimerButton } from "@/components/SleepTimerButton";
import { useIsDownloaded } from "@/hooks/useDownloadedSet";
import { useRecordPlays } from "@/hooks/useRecentlyPlayed";
import { usePlayerActions, usePlayerMeta, usePlayerTime } from "@/hooks/PlayerContext";
import { cn } from "@/lib/utils";

export function NowPlaying({
  onToggleQueue,
  onToggleLyrics,
  onExpand,
  onDownload,
}: {
  onToggleQueue: () => void;
  onToggleLyrics: () => void;
  onExpand: () => void;
  onDownload: OnDownload;
}) {
  const { track, playing, loading, error, volume, shuffle, repeat, hasNext, hasPrev } = usePlayerMeta();
  const { currentTime, duration } = usePlayerTime();
  const actions = usePlayerActions();
  const isLocal = useIsDownloaded(track?.id ?? "");
  // Record plays from here — NowPlaying already re-renders on every
  // timeupdate (via PlayerTime context), so subscribing here is free.
  useRecordPlays(track, currentTime);
  if (!track) return null;

  const cover = imageProxy(track.album?.cover);
  const pct = duration > 0 ? (currentTime / duration) * 100 : 0;

  return (
    <div className="border-t border-border bg-[#0a0a0a] px-4 py-3">
      <div className="flex items-center gap-4">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <button
            onClick={onExpand}
            className="group relative h-14 w-14 flex-shrink-0 overflow-hidden rounded bg-secondary"
            title="Expand now playing"
          >
            {cover ? (
              <img src={cover} alt="" className="h-full w-full object-cover" />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-muted-foreground">
                <Music className="h-5 w-5" />
              </div>
            )}
            <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
              <svg className="h-4 w-4 text-foreground" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M15 3h6v6M14 10l7-7M9 21H3v-6M10 14l-7 7" />
              </svg>
            </span>
          </button>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">
              {track.album ? (
                <Link to={`/album/${track.album.id}`} className="hover:underline">
                  {track.name}
                </Link>
              ) : (
                track.name
              )}
            </div>
            <div className="truncate text-xs text-muted-foreground">
              {track.artists.map((a, i) => (
                <span key={a.id}>
                  {i > 0 && ", "}
                  <Link to={`/artist/${a.id}`} className="hover:underline">
                    {a.name}
                  </Link>
                </span>
              ))}
              {isLocal ? (
                <span className="ml-2 rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-primary">
                  Lossless
                </span>
              ) : (
                <span className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase tracking-wider">
                  Preview
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center">
            <HeartButton kind="track" id={track.id} size="sm" />
            <DownloadButton kind="track" id={track.id} onPick={onDownload} iconOnly variant="ghost" />
          </div>
        </div>

        <div className="flex flex-1 flex-col items-center gap-1.5">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              className={cn("h-8 w-8", shuffle && "text-primary")}
              onClick={actions.toggleShuffle}
              title={shuffle ? "Shuffle on" : "Shuffle off"}
            >
              <Shuffle className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={actions.prev}
              disabled={!hasPrev && currentTime < 3}
              title="Previous"
            >
              <SkipBack className="h-4 w-4" fill="currentColor" />
            </Button>
            <Button
              size="icon"
              onClick={actions.toggle}
              className="h-9 w-9 rounded-full"
              disabled={loading && !playing}
              title={playing ? "Pause" : "Play"}
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : playing ? (
                <Pause className="h-4 w-4" fill="currentColor" />
              ) : (
                <Play className="h-4 w-4" fill="currentColor" />
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={actions.next}
              disabled={!hasNext && !shuffle && repeat === "off"}
              title="Next"
            >
              <SkipForward className="h-4 w-4" fill="currentColor" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className={cn("h-8 w-8", repeat !== "off" && "text-primary")}
              onClick={actions.cycleRepeat}
              title={
                repeat === "off"
                  ? "Repeat off"
                  : repeat === "all"
                    ? "Repeat all"
                    : "Repeat one"
              }
              aria-label={`Repeat: ${repeat}`}
            >
              {repeat === "one" ? (
                <Repeat1 className="h-4 w-4" />
              ) : (
                <Repeat className="h-4 w-4" />
              )}
            </Button>
          </div>
          <div className="flex w-full max-w-xl items-center gap-2 text-[11px] text-muted-foreground">
            <span className="w-10 text-right tabular-nums">{formatDuration(currentTime)}</span>
            <input
              type="range"
              min={0}
              max={Math.max(duration, 1)}
              step={0.1}
              value={Math.min(currentTime, duration || 0)}
              onChange={(e) => actions.seek(Number(e.target.value))}
              className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-secondary accent-primary"
              style={{
                background: `linear-gradient(to right, hsl(var(--primary)) ${pct}%, hsl(var(--secondary)) ${pct}%)`,
              }}
              aria-label="Seek"
            />
            <span className="w-10 tabular-nums">{formatDuration(duration)}</span>
          </div>
        </div>

        <div className="flex flex-1 items-center justify-end gap-2">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={onToggleLyrics}
            title="Lyrics"
          >
            <Mic2 className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={onToggleQueue}
            title="Show queue"
          >
            <ListMusic className="h-4 w-4" />
          </Button>
          <SleepTimerButton />
          <VolumeControl value={volume} onChange={actions.setVolume} />
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={actions.stop}
            title="Close player"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>
      {error && (
        <div className="mt-1 text-center text-[11px] text-destructive">{error}</div>
      )}
    </div>
  );
}

function VolumeControl({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const muted = value === 0;
  return (
    <div className="flex items-center gap-2">
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8"
        onClick={() => onChange(muted ? 1 : 0)}
        title={muted ? "Unmute" : "Mute"}
      >
        {muted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
      </Button>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1 w-20 cursor-pointer appearance-none rounded-full bg-secondary accent-primary"
        style={{
          background: `linear-gradient(to right, hsl(var(--primary)) ${value * 100}%, hsl(var(--secondary)) ${value * 100}%)`,
        }}
        aria-label="Volume"
      />
    </div>
  );
}
