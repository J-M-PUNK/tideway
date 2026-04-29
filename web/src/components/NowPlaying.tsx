import { useState } from "react";
import { Link } from "react-router-dom";
import {
  AudioLines,
  ListMusic,
  Loader2,
  Mic2,
  MoreHorizontal,
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
} from "lucide-react";
import type { OnDownload } from "@/api/download";
import type { StreamInfo } from "@/api/types";
import { formatDuration, imageProxy } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  CONTEXT_MENU_PARTS,
  DROPDOWN_MENU_PARTS,
  TrackMenuItems,
} from "@/components/TrackMenu";
import { CreditsDialog } from "@/components/CreditsDialog";
import { DownloadButton } from "@/components/DownloadButton";
import { HeartButton } from "@/components/HeartButton";
import { OutputDevicePicker } from "@/components/OutputDevicePicker";
import { SleepTimerButton } from "@/components/SleepTimerButton";
import { StreamQualityBadge } from "@/components/StreamQualityBadge";
import { useIsDownloaded } from "@/hooks/useDownloadedSet";
import { useRecordPlays } from "@/hooks/useRecentlyPlayed";
import {
  usePlayerActions,
  usePlayerMeta,
  usePlayerTime,
} from "@/hooks/PlayerContext";
import {
  useUiPreferences,
  type StreamingQuality,
} from "@/hooks/useUiPreferences";
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
  const {
    track,
    playing,
    loading,
    error,
    volume,
    shuffle,
    repeat,
    hasNext,
    hasPrev,
    queue,
    streamInfo,
    forceVolume,
  } = usePlayerMeta();
  const actions = usePlayerActions();
  const isLocal = useIsDownloaded(track?.id ?? "");
  // Shared credits-dialog state, opened by the right-click menu on the
  // current-track info block. Kept at this level so closing the menu
  // doesn't tear down the dialog.
  const [creditsOpen, setCreditsOpen] = useState(false);
  // When nothing's loaded we still render the bar so the output-device
  // picker + volume control + queue button are always reachable. The
  // body is just a lighter empty state.
  if (!track) return <EmptyPlayerBar onToggleQueue={onToggleQueue} />;

  const cover = imageProxy(track.album?.cover);

  return (
    <div className="border-t border-border bg-[hsl(var(--now-playing-bg))] px-4 py-3">
      <div className="flex items-center gap-4">
        <ContextMenu>
          <ContextMenuTrigger asChild>
            <div className="flex min-w-0 flex-1 items-center gap-3">
              <button
                onClick={onExpand}
                className="group relative h-14 w-14 flex-shrink-0 overflow-hidden rounded bg-secondary"
                title="Expand now playing"
              >
                {cover ? (
                  <img
                    src={cover}
                    alt=""
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-muted-foreground">
                    <Music className="h-5 w-5" />
                  </div>
                )}
                <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
                  <svg
                    className="h-4 w-4 text-foreground"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                  >
                    <path d="M15 3h6v6M14 10l7-7M9 21H3v-6M10 14l-7 7" />
                  </svg>
                </span>
              </button>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold">
                  {track.album ? (
                    <Link
                      to={`/album/${track.album.id}`}
                      className="hover:underline"
                    >
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
                  {isLocal && (
                    <span className="ml-2 rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-primary">
                      Downloaded
                    </span>
                  )}
                  <StreamQualityBadge info={streamInfo} className="ml-2" />
                </div>
              </div>
              <div className="flex items-center">
                <HeartButton
                  kind="track"
                  id={track.id}
                  size="sm"
                  tone="foreground"
                />
                <DownloadButton
                  kind="track"
                  id={track.id}
                  onPick={onDownload}
                  iconOnly
                  variant="ghost"
                  mediaTags={track.media_tags}
                />
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 data-[state=open]:text-primary"
                      title="More"
                      aria-label="Track actions"
                    >
                      <MoreHorizontal className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-60">
                    <TrackMenuItems
                      parts={DROPDOWN_MENU_PARTS}
                      track={track}
                      context={queue.length > 0 ? queue : [track]}
                      onDownload={onDownload}
                      onShowCredits={() => setCreditsOpen(true)}
                      showSelect={false}
                    />
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>
          </ContextMenuTrigger>
          <ContextMenuContent className="w-60">
            <TrackMenuItems
              parts={CONTEXT_MENU_PARTS}
              track={track}
              context={queue.length > 0 ? queue : [track]}
              onDownload={onDownload}
              onShowCredits={() => setCreditsOpen(true)}
              showSelect={false}
            />
          </ContextMenuContent>
        </ContextMenu>
        <CreditsDialog
          trackId={track.id}
          trackName={track.name}
          open={creditsOpen}
          onOpenChange={setCreditsOpen}
        />

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
              disabled={!hasPrev}
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
          <ProgressBar track={track} />
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
          <StreamingQualityPicker isLocal={isLocal} streamInfo={streamInfo} />
          <SleepTimerButton />
          <OutputDevicePicker />
          <VolumeControl
            value={volume}
            onChange={actions.setVolume}
            disabled={forceVolume}
          />
        </div>
      </div>
      {error && (
        <div className="mt-1 text-center text-[11px] text-destructive">
          {error}
        </div>
      )}
    </div>
  );
}

/**
 * Progress bar, elapsed / remaining time, and the `useRecordPlays`
 * subscription. Split out from the main NowPlaying body so the
 * bottom bar's expensive parts — cover, title, artist link, side
 * buttons — don't re-render at the player's 4 Hz position tick.
 * Only this tiny subtree consumes `usePlayerTime` and gets ticked.
 */
function ProgressBar({
  track,
}: {
  track: NonNullable<ReturnType<typeof usePlayerMeta>["track"]>;
}) {
  const { currentTime, duration } = usePlayerTime();
  const actions = usePlayerActions();
  useRecordPlays(track, currentTime);
  const pct = duration > 0 ? (currentTime / duration) * 100 : 0;
  return (
    <div className="flex w-full max-w-xl items-center gap-2 text-[11px] text-muted-foreground">
      <span className="w-10 text-right tabular-nums">
        {formatDuration(currentTime)}
      </span>
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
  );
}

const QUALITY_OPTIONS: {
  value: StreamingQuality;
  label: string;
  sublabel: string;
}[] = [
  { value: "low_96k", label: "Low", sublabel: "96 kbps" },
  { value: "low_320k", label: "Medium", sublabel: "320 kbps" },
  { value: "high_lossless", label: "High", sublabel: "Lossless (16-bit)" },
  { value: "hi_res_lossless", label: "Max", sublabel: "Up to 24-bit, 192 kHz" },
];

// Per-tier color for the Now Playing quality pill. Pinned Tailwind
// palette entries read on both dark and light backgrounds without
// needing a mode-specific override. `primary` is the brand accent,
// reserved for Max so the top tier is visually distinct.
const QUALITY_BADGE_CLASS: Record<StreamingQuality, string> = {
  low_96k: "bg-neutral-500/15 text-neutral-400 hover:bg-neutral-500/25",
  low_320k: "bg-amber-500/15 text-amber-500 hover:bg-amber-500/25",
  high_lossless: "bg-sky-500/15 text-sky-500 hover:bg-sky-500/25",
  hi_res_lossless: "bg-primary/15 text-primary hover:bg-primary/25",
};

/**
 * Map a Tidal `audio_quality` string back to one of our four
 * StreamingQuality buckets. Lets the picker pill reflect what
 * Tidal is actually delivering — so when the user has Max picked
 * but the current track only ships at Lossless, the pill reads
 * "High" instead of misleadingly claiming "Max" alongside the
 * StreamQualityBadge that already shows "High".
 */
function streamingQualityFromAudioQuality(
  aq: string | null | undefined,
): StreamingQuality | null {
  const v = (aq || "").toUpperCase();
  if (v === "LOW") return "low_96k";
  if (v === "HIGH") return "low_320k";
  if (v === "LOSSLESS") return "high_lossless";
  if (v === "HI_RES" || v === "HI_RES_LOSSLESS") return "hi_res_lossless";
  return null;
}

/**
 * Quality picker pill on the Now Playing bar. When the current track is
 * a downloaded local file, this is a no-op badge (playback is already at
 * the file's native quality) — only the streaming path actually switches.
 *
 * The pill LABEL reflects what Tidal is actually streaming right now,
 * not the user's preferred ceiling. A track that only ships at
 * LOSSLESS shows "High" even when the user has Max selected as their
 * preference, so the picker stays consistent with the
 * StreamQualityBadge ("High") and the album hero badge ("Lossless")
 * for the same release. The dropdown still shows the user's
 * preference highlighted with a "Current" tag so the choice itself
 * is never ambiguous; switching is a one-click change away.
 */
function StreamingQualityPicker({
  isLocal,
  streamInfo,
}: {
  isLocal: boolean;
  streamInfo: StreamInfo | null;
}) {
  const { streamingQuality, set } = useUiPreferences();
  // What's audible right now. Falls back to the user's preference
  // when we have no stream info (idle, mid-load) — better than
  // showing "Streaming" placeholder text while the bar is mounted.
  const effectiveQuality =
    streamingQualityFromAudioQuality(streamInfo?.audio_quality) ??
    streamingQuality;
  const current = QUALITY_OPTIONS.find((q) => q.value === effectiveQuality);
  const label = isLocal ? "Downloaded" : (current?.label ?? "Streaming");

  if (isLocal) {
    return (
      <div
        className="flex items-center gap-1.5 rounded-full bg-primary/15 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-primary"
        title="Playing the local file at its downloaded quality"
      >
        <AudioLines className="h-3 w-3" /> {label}
      </div>
    );
  }
  const badgeClass = QUALITY_BADGE_CLASS[effectiveQuality];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className={cn(
            "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider transition-colors",
            badgeClass,
          )}
          title="Streaming quality"
        >
          <AudioLines className="h-3 w-3" /> {label}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel>Streaming quality</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {QUALITY_OPTIONS.map((q) => {
          const isPreference = q.value === streamingQuality;
          const isPlaying = q.value === effectiveQuality;
          return (
            <DropdownMenuItem
              key={q.value}
              onSelect={() => set({ streamingQuality: q.value })}
            >
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "font-semibold",
                      isPreference && "text-primary",
                    )}
                  >
                    {q.label}
                  </span>
                  {isPreference && (
                    <span className="text-[10px] font-medium uppercase tracking-wider text-primary">
                      Selected
                    </span>
                  )}
                  {isPlaying && !isPreference && (
                    <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                      Playing
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">
                  {q.sublabel}
                </div>
              </div>
            </DropdownMenuItem>
          );
        })}
        <DropdownMenuSeparator />
        <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
          The pill shows what Tidal is delivering for this track. If the release
          doesn't go up to your selected tier, you'll get the highest tier it
          does have.
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function VolumeControl({
  value,
  onChange,
  disabled,
}: {
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  const muted = value === 0;
  return (
    <div
      className={cn("flex items-center gap-2", disabled && "opacity-50")}
      title={
        disabled
          ? "Force Volume is on — attenuate on your output device"
          : undefined
      }
    >
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8"
        onClick={() => onChange(muted ? 1 : 0)}
        disabled={disabled}
        title={muted ? "Unmute" : "Mute"}
      >
        {muted ? (
          <VolumeX className="h-4 w-4" />
        ) : (
          <Volume2 className="h-4 w-4" />
        )}
      </Button>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        disabled={disabled}
        className="h-1 w-20 cursor-pointer appearance-none rounded-full bg-secondary accent-primary disabled:cursor-not-allowed"
        style={{
          background: `linear-gradient(to right, hsl(var(--primary)) ${value * 100}%, hsl(var(--secondary)) ${value * 100}%)`,
        }}
        aria-label="Volume"
      />
    </div>
  );
}

/**
 * Rendered when nothing is playing. Keeps the output-device picker
 * and queue button reachable so users can set things up before
 * starting playback, and gives the main layout a constant footer
 * height so the viewport doesn't jump when the first track loads.
 */
function EmptyPlayerBar({ onToggleQueue }: { onToggleQueue: () => void }) {
  return (
    <div className="border-t border-border bg-[hsl(var(--now-playing-bg))] px-4 py-3">
      <div className="flex items-center gap-4">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className="flex h-14 w-14 flex-shrink-0 items-center justify-center rounded bg-secondary text-muted-foreground">
            <Music className="h-5 w-5" />
          </div>
          <div className="min-w-0 text-sm text-muted-foreground">
            Nothing playing
          </div>
        </div>
        <div className="flex flex-1 items-center justify-end gap-2">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={onToggleQueue}
            title="Show queue"
          >
            <ListMusic className="h-4 w-4" />
          </Button>
          <OutputDevicePicker />
        </div>
      </div>
    </div>
  );
}
