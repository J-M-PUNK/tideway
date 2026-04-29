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
import { CastPicker } from "@/components/CastPicker";
import { SleepTimerButton } from "@/components/SleepTimerButton";
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
              {/*
                Track-change crossfade. Keying the cover + title block
                on track.id makes React replay the `animate-track-change`
                animation every time the queue advances; same track
                (loop / restart) keeps the same key and reuses the
                element without re-animating. Subtle fade-in-from-2px
                so the bar reads as "the new track slid into place"
                rather than "the bar redrew."
              */}
              <button
                key={`cover-${track.id}`}
                onClick={onExpand}
                className="group relative h-14 w-14 flex-shrink-0 animate-track-change overflow-hidden rounded bg-secondary"
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
              <div
                key={`meta-${track.id}`}
                className="min-w-0 animate-track-change"
              >
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
          <CastPicker />
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
    <div className="group flex w-full max-w-xl items-center gap-2 text-[11px] text-muted-foreground">
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
        // Thicken from h-1 to h-1.5 when the row is hovered. The
        // `group` modifier on the wrapper means the seek bar grows
        // any time the cursor is anywhere over the time row, which
        // matches the "the bar invites scrubbing" UX. transition
        // covers the height change so the bar swells / settles
        // instead of snapping.
        className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-secondary accent-primary transition-[height] duration-150 ease-out group-hover:h-1.5"
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
 * StreamingQuality buckets. Used to colour-code the picker pill by
 * the tier that's actually playing, even when it differs from the
 * user's preference (e.g. a Lossless-only release while the user
 * has Max selected).
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
 * Format the kHz portion of the pill label. 44100 → "44.1", 96000
 * → "96", 192000 → "192" — drop the ".0" off whole-number kHz
 * values so the pill stays compact.
 */
function formatKhz(hz: number | null | undefined): string | null {
  if (!hz || hz <= 0) return null;
  const k = hz / 1000;
  return k % 1 === 0 ? String(k) : k.toFixed(1);
}

/**
 * Build the user-visible pill label from a StreamInfo. The codec
 * is dropped from the readout — the tier-coded pill color
 * (primary for hi-res, sky for lossless, amber/neutral for lossy)
 * already carries the codec-class signal, and the rate / depth
 * tells the user what they actually want to know about the stream
 * they're listening to. Pieces that aren't available (lossy
 * streams have no bit_depth) are dropped so the result reads
 * cleanly.
 *
 *   FLAC streaming Hi-Res → "96kHz · 24-bit"
 *   FLAC CD-res lossless  → "44.1kHz · 16-bit"
 *   AAC 320               → "44.1kHz"
 *   Local M4A             → "44.1kHz"
 *
 * Returns null when we don't have any usable info — caller should
 * render a placeholder ("Streaming"/"Loading") rather than an empty
 * pill in that case.
 */
function streamInfoFullLabel(info: StreamInfo | null): string | null {
  if (!info) return null;
  const rate = formatKhz(info.sample_rate_hz);
  const depth = info.bit_depth ? `${info.bit_depth}-bit` : null;
  const parts = [rate ? `${rate}kHz` : null, depth].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/**
 * Quality pill on the Now Playing bar. The pill shows the FULL
 * codec / rate / depth readout for what's currently audible —
 * Tidal's own desktop uses the same pattern in its now-playing
 * chrome. Click to open the dropdown and change the streaming-
 * quality preference (this only affects future tracks, since
 * Tidal returns a fresh stream URL per quality tier per track).
 *
 * The pill colour is still tier-coded — primary for Max, sky for
 * Lossless, amber for Medium, neutral for Low — so the at-a-glance
 * "what kind of stream is this" signal stays even when the readout
 * itself is dense codec text.
 */
function StreamingQualityPicker({
  isLocal,
  streamInfo,
}: {
  isLocal: boolean;
  streamInfo: StreamInfo | null;
}) {
  const { streamingQuality, set } = useUiPreferences();
  // What's audible right now drives the colour and the dropdown's
  // "Playing" tag. When stream_info is missing (idle or mid-load)
  // we fall back to the user's preference so the pill still has a
  // sensible tone.
  const effectiveQuality =
    streamingQualityFromAudioQuality(streamInfo?.audio_quality) ??
    streamingQuality;
  // Pill text: the full codec/rate/depth readout. While loading or
  // idle we show the user's preference label — rendering "Streaming"
  // would be a placeholder that doesn't tell anyone anything.
  const fullLabel = streamInfoFullLabel(streamInfo);
  const fallbackLabel =
    QUALITY_OPTIONS.find((q) => q.value === effectiveQuality)?.label ??
    "Streaming";
  const pillLabel = fullLabel ?? fallbackLabel;

  if (isLocal) {
    return (
      <div
        className="flex items-center gap-1.5 rounded-full bg-primary/15 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-primary"
        title="Playing the local file at its downloaded quality"
      >
        <AudioLines className="h-3 w-3" /> {pillLabel}
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
          <AudioLines className="h-3 w-3" /> {pillLabel}
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
      className={cn("group flex items-center gap-2", disabled && "opacity-50")}
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
        // Same hover-thicken pattern as the seek bar above. `group`
        // on the wrapper means the slider grows whenever the cursor
        // is anywhere in the volume row, including when hovering
        // the mute button — invites adjustment without making the
        // user pixel-hunt the 4px track.
        className="h-1 w-20 cursor-pointer appearance-none rounded-full bg-secondary accent-primary transition-[height] duration-150 ease-out group-hover:h-1.5 disabled:cursor-not-allowed"
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
