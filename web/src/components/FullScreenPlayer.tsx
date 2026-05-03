import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  ChevronDown,
  Info,
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
  Sparkles,
} from "lucide-react";
import type { OnDownload } from "@/api/download";
import type { CreditEntry, Lyrics, StreamInfo } from "@/api/types";
import { useAutoEqState } from "@/hooks/useAutoEqState";
import { useCoverColor } from "@/hooks/useCoverColor";
import { useCredits } from "@/hooks/useCredits";
import { useIsDownloaded } from "@/hooks/useDownloadedSet";
import { useLyrics } from "@/hooks/useLyrics";
import {
  usePlayerActions,
  usePlayerMeta,
  usePlayerTime,
} from "@/hooks/PlayerContext";
import { Button } from "@/components/ui/button";
import { HeartButton } from "@/components/HeartButton";
import { DownloadButton } from "@/components/DownloadButton";
import { cn, formatDuration, imageProxy } from "@/lib/utils";

// Right-pane tab in the full-screen player. Matches the affordance
// the official Tidal desktop client surfaces in the same view —
// users dragged from Tidal expect Lyrics / Credits / Similar to
// be one click apart from each other rather than buried in
// separate dialogs.
type RightPaneTab = "similar" | "credits" | "lyrics";

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
  const { track, playing, loading, shuffle, repeat, hasPrev, streamInfo } =
    usePlayerMeta();
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

  const [activeTab, setActiveTab] = useState<RightPaneTab>("lyrics");

  // Shared with LyricsPanel — same hook, same cache, so opening one view
  // doesn't refetch what the other already has.
  const { lyrics, loading: loadingLyrics } = useLyrics(
    open ? (track?.id ?? null) : null,
  );
  const activeIdx = useActiveLyric(lyrics, currentTime);

  // Credits hook only fires when the credits tab is selected, so
  // the network call is deferred for the common case where the
  // user opens the full-screen player just to see the cover.
  const { credits, loading: loadingCredits } = useCredits(
    open ? (track?.id ?? null) : null,
    open && activeTab === "credits",
  );

  if (!open || !track) return null;

  // Tidal's full-screen view sits on a single muted cover-derived
  // tone rather than fading to black. Using `dominant` directly with
  // a slight bottom darken keeps the controls bar legible without
  // killing the "the room is the album cover" mood. Falls back to
  // the old neutral gradient if the dominant-color extraction is
  // still pending or failed.
  const bg = dominant
    ? `linear-gradient(180deg, ${dominant} 0%, ${dominant} 70%, color-mix(in srgb, ${dominant} 70%, black) 100%)`
    : "linear-gradient(180deg, hsl(0 0% 20%), hsl(0 0% 5%))";

  return (
    <div
      className="fixed inset-0 z-[70] flex flex-col text-foreground transition-opacity"
      style={{ background: bg }}
      role="dialog"
      aria-modal="true"
    >
      <div className="flex items-center justify-between gap-4 px-6 py-4">
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
        <div className="flex items-center gap-2">
          <RightPaneTabs active={activeTab} onChange={setActiveTab} />
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
          {activeTab === "lyrics" &&
            (lyrics && lyrics.synced && lyrics.synced.length > 0 ? (
              <SyncedLyricsPane
                lines={lyrics.synced}
                active={activeIdx}
                onSeek={actions.seek}
              />
            ) : lyrics?.text ? (
              <pre className="max-h-[55vh] overflow-y-auto whitespace-pre-wrap font-sans text-lg leading-relaxed text-foreground/80 scrollbar-thin">
                {lyrics.text}
              </pre>
            ) : loadingLyrics ? (
              <div className="flex items-center gap-2 text-foreground/60">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading lyrics…
              </div>
            ) : (
              <LyricsPlaceholder />
            ))}
          {activeTab === "credits" && (
            <CreditsPane
              credits={credits}
              loading={loadingCredits}
              onArtistClick={onClose}
            />
          )}
          {activeTab === "similar" && <SimilarPlaceholder />}
        </div>
      </div>

      <SignalPathStrip open={open} streamInfo={streamInfo} />

      {/* Footer controls */}
      <div className="border-t border-white/10 bg-black/30 px-6 py-4 backdrop-blur-sm">
        <div className="flex items-center gap-4">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <div className="min-w-0">
              <div className="truncate text-base font-semibold">
                {track.name}
              </div>
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
              mediaTags={track.media_tags}
            />
          </div>

          <div className="flex flex-1 flex-col items-center gap-2">
            <div className="flex items-center gap-3">
              <Button
                variant="ghost"
                size="icon"
                onClick={actions.toggleShuffle}
                className={cn(
                  "h-9 w-9 hover:bg-white/10",
                  shuffle && "text-primary",
                )}
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
                className="h-1 flex-1 cursor-pointer appearance-none rounded-full accent-primary"
                style={{
                  background: `linear-gradient(to right, white ${pct}%, rgba(255,255,255,0.2) ${pct}%)`,
                }}
                aria-label="Seek"
              />
              <span className="w-10 tabular-nums">
                {formatDuration(duration)}
              </span>
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
                isActive
                  ? "text-foreground"
                  : "text-foreground hover:text-foreground",
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

const TAB_LABELS: { id: RightPaneTab; label: string }[] = [
  { id: "similar", label: "Similar tracks" },
  { id: "credits", label: "Credits" },
  { id: "lyrics", label: "Lyrics" },
];

function RightPaneTabs({
  active,
  onChange,
}: {
  active: RightPaneTab;
  onChange: (tab: RightPaneTab) => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      {TAB_LABELS.map((t) => {
        const isActive = active === t.id;
        return (
          <button
            key={t.id}
            type="button"
            onClick={() => onChange(t.id)}
            aria-pressed={isActive}
            className={cn(
              "rounded-full px-4 py-1.5 text-sm font-semibold transition-colors",
              isActive
                ? "bg-white text-black"
                : "bg-black/25 text-foreground/85 hover:bg-black/40 hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

function CreditsPane({
  credits,
  loading,
  onArtistClick,
}: {
  credits: CreditEntry[] | null;
  loading: boolean;
  onArtistClick: () => void;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-foreground/60">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading credits…
      </div>
    );
  }
  if (!credits || credits.length === 0) {
    return (
      <div className="flex items-center gap-3 rounded-lg bg-white/5 p-6 text-sm text-foreground/60">
        <Info className="h-5 w-5 flex-shrink-0" />
        <span>No credits listed for this track.</span>
      </div>
    );
  }
  return (
    <div className="max-h-[55vh] overflow-y-auto pr-2 scrollbar-thin">
      <div className="flex flex-col gap-5">
        {credits.map((entry) => (
          <div key={entry.role} className="flex flex-col gap-1">
            <div className="text-xs font-semibold uppercase tracking-wider text-foreground/60">
              {entry.role}
            </div>
            <div className="text-base leading-snug">
              {entry.contributors.map((c, i) => (
                <span key={`${c.name}-${i}`}>
                  {i > 0 && <span className="text-foreground/50">, </span>}
                  {c.id ? (
                    <Link
                      to={`/artist/${c.id}`}
                      onClick={onArtistClick}
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
    </div>
  );
}

function SimilarPlaceholder() {
  // Stubbed for the v1 reskin. Backend exposes search-time
  // "similar artists" today but no per-track similar-tracks
  // endpoint yet — adding that is a follow-up (likely a thin
  // wrapper around tidalapi's track radio). The placeholder
  // tells the user it's coming rather than rendering an empty
  // panel that reads as broken.
  return (
    <div className="flex items-start gap-3 rounded-lg bg-white/5 p-6 text-sm text-foreground/70">
      <Sparkles className="mt-0.5 h-5 w-5 flex-shrink-0" />
      <div className="flex flex-col gap-1">
        <div className="font-semibold text-foreground">Similar tracks</div>
        <p className="text-foreground/60">
          Coming in a future release — a per-track radio fed by Tidal&apos;s
          recommendation graph, surfaced alongside lyrics and credits so you can
          chase a vibe without leaving the now-playing view.
        </p>
      </div>
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

/**
 * One-line "what's actually happening to the audio" summary plus
 * an A/B bypass button — Phase 4 of the AutoEQ scope. Reads
 * stream codec / sample rate from the player snapshot and EQ
 * mode / profile / bypass from the AutoEQ state endpoint.
 *
 * Hidden when the AutoEQ state endpoint isn't reachable (older
 * server build) — the strip is purely informational and
 * shouldn't take down the FullScreenPlayer if it can't load.
 *
 * Only fetches state while the player is open — no point
 * polling the EQ state when the user can't see the strip.
 */
function SignalPathStrip({
  open,
  streamInfo,
}: {
  open: boolean;
  streamInfo: StreamInfo | null;
}) {
  const { state, setBypass } = useAutoEqState(open);
  if (state === null) return null;

  const eqLabel =
    state.mode === "off" || !state.enabled
      ? "EQ off"
      : state.mode === "profile" && state.active_profile
        ? `${state.active_profile.brand} ${state.active_profile.model}`
        : state.mode === "manual"
          ? "Manual EQ"
          : "EQ off";

  // Only show the bypass button when an EQ is actually active —
  // there's nothing to A/B compare when EQ is off / no profile.
  const eqIsActive =
    state.enabled &&
    state.mode !== "off" &&
    (state.mode === "manual" || state.active_profile !== null);

  const formatLabel = streamInfo
    ? `${streamInfo.codec || "?"}${
        streamInfo.sample_rate_hz
          ? ` ${(streamInfo.sample_rate_hz / 1000).toFixed(streamInfo.sample_rate_hz % 1000 === 0 ? 0 : 1)}kHz`
          : ""
      }${streamInfo.bit_depth ? ` / ${streamInfo.bit_depth}-bit` : ""}`
    : null;

  return (
    <div className="flex flex-wrap items-center justify-center gap-3 border-t border-white/5 bg-black/20 px-6 py-2 text-[11px] text-foreground/60">
      <div className="flex flex-wrap items-center gap-2 truncate font-mono">
        {formatLabel && (
          <>
            <span>{formatLabel}</span>
            <span className="text-foreground/30">→</span>
          </>
        )}
        <span
          className={cn(
            state.bypass
              ? "text-foreground/40 line-through"
              : eqIsActive
                ? "text-foreground/90"
                : "text-foreground/60",
          )}
        >
          {eqLabel}
        </span>
      </div>
      {eqIsActive && (
        <button
          type="button"
          onClick={() => void setBypass(!state.bypass)}
          className={cn(
            "rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider transition-colors",
            state.bypass
              ? "bg-foreground/10 text-foreground/60 hover:bg-foreground/20"
              : "bg-primary text-primary-foreground hover:bg-primary/80",
          )}
          title="Toggle EQ on / off for A/B comparison"
        >
          {state.bypass ? "EQ Bypassed" : "EQ On"}
        </button>
      )}
    </div>
  );
}
