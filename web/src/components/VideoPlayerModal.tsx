import { useCallback, useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { Link, useNavigate } from "react-router-dom";
import {
  ChevronDown,
  Copy,
  ExternalLink,
  FileText,
  Info,
  Loader2,
  Maximize2,
  MoreHorizontal,
  Pause,
  PictureInPicture2,
  Play,
  Repeat,
  Repeat1,
  Shuffle,
  SkipBack,
  SkipForward,
  User,
  Video as VideoIcon,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import { api } from "@/api/client";
import type { CreditEntry, Video } from "@/api/types";
import { useVideoPlayer } from "@/hooks/useVideoPlayer";
import { useVideoStream } from "@/hooks/useVideoStream";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { useToast } from "@/components/toast";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { VideoDownloadButton } from "@/components/VideoDownloadButton";
import { cn, formatDuration, imageProxy } from "@/lib/utils";

/**
 * Full-screen music-video modal modeled after Tidal's desktop player.
 *
 * Top bar: Similar videos panel toggle, Credits, Picture-in-Picture,
 * Fullscreen, Minimize.
 *
 * Video surface: `<video>` with HLS driven natively (Safari / WKWebView)
 * or via hls.js (Chrome / Firefox / Edge). Cover art is used as the
 * `poster` so users see the thumbnail instantly while segments load.
 * Vertical volume slider on the right side mimics Tidal's overlay.
 *
 * Bottom bar: track info + heart/more on the left, shuffle/prev/play/
 * next/repeat in the center, queue/vol/quality on the right. Full
 * progress bar + timestamps across the bottom edge.
 *
 * Keyboard: Esc closes, Space toggles play/pause, ←/→ scrub 5s,
 * ↑/↓ adjust volume, F toggles fullscreen.
 */
export function VideoPlayerModal() {
  const { current, queue, queueIndex, close, next, prev, hasNext, hasPrev, open } =
    useVideoPlayer();
  // Default to HIGH (1080p). Chrome/Firefox/Safari all handle a
  // 1080p HLS manifest fine on modern hardware and this matches
  // Tidal's own desktop-client default.
  const [quality, setQuality] = useState<string | undefined>("HIGH");
  const { url, error, loading } = useVideoStream(current?.id ?? null, quality);
  const playerActions = usePlayerActions();
  const { playing: audioPlaying } = usePlayerMeta();

  const [playing, setPlaying] = useState(true);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(() => readVolume());
  const [muted, setMuted] = useState(false);
  const [shuffle, setShuffle] = useState(false);
  const [repeat, setRepeat] = useState(false);
  const [showSimilar, setShowSimilar] = useState(false);
  const [showCredits, setShowCredits] = useState(false);
  const [minimized, setMinimized] = useState(false);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const modalRef = useRef<HTMLDivElement | null>(null);
  const wasAudioPlayingRef = useRef(false);
  const modalOpen = !!current;

  // Pause audio when the modal opens (first time a video appears) and
  // restore when it closes (video goes to null). Keyed on `modalOpen`
  // — NOT on `current?.id` — so navigating between videos via
  // next/prev doesn't momentarily toggle audio on and off again.
  useEffect(() => {
    if (!modalOpen) return;
    wasAudioPlayingRef.current = audioPlaying;
    if (audioPlaying) playerActions.toggle();
    return () => {
      if (wasAudioPlayingRef.current) playerActions.toggle();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modalOpen]);

  // Reset per-video state when switching tracks so the progress bar
  // and duration readout don't flash stale values from the previous
  // video while the new one loads.
  useEffect(() => {
    setCurrentTime(0);
    setDuration(0);
    setPlaying(true);
  }, [current?.id]);

  // Persist volume across sessions.
  useEffect(() => {
    try {
      localStorage.setItem("tidal-downloader:video-volume", String(volume));
    } catch {
      /* ignore quota */
    }
  }, [volume]);

  // Attach the HLS manifest to the <video> element. Safari and
  // macOS WKWebView can decode application/vnd.apple.mpegurl
  // directly; Chrome / Edge / Firefox cannot, so hls.js ships
  // segment-level media via MediaSourceExtensions instead.
  //
  // Autoplay: the <video>'s `autoPlay` attribute fires when the
  // element acquires a playable source. On the Safari path that's
  // the `src` assignment below. On the hls.js path there's no
  // `src` — MediaSource is attached via attachMedia — so autoplay
  // doesn't trigger automatically. The canonical fix is to call
  // `.play()` from `MANIFEST_PARSED`, which is the earliest point
  // hls.js has told the <video> about the stream.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !url) return;
    // Prefer hls.js when supported. Chrome lies about
    // `canPlayType('application/vnd.apple.mpegurl')` — returns
    // 'maybe' when it actually can't decode HLS — so native-first
    // branching silently breaks Chrome. Safari doesn't support
    // MediaSource for HLS, so `Hls.isSupported()` returns false
    // there and we fall through to the native path.
    if (Hls.isSupported()) {
      const hls = new Hls();
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        // Start muted on the hls.js path. Chromium's autoplay
        // policy blocks audible autoplay without a recent user-
        // gesture credit propagating to this async callback.
        // Muted autoplay is universally allowed; one click
        // unmutes. Same UX TikTok / Twitter / Instagram use.
        video.muted = true;
        setMuted(true);
        video.play().catch(() => {
          // Even muted autoplay failed (iOS Low-Power Mode,
          // locked-down enterprise policy). Show play button.
          setPlaying(false);
        });
      });
      return () => {
        hls.destroy();
      };
    }
    // Native HLS path (Safari / WKWebView). canPlayType returns
    // 'probably' or 'maybe' — either is a go.
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      return;
    }
    // Truly no HLS path — assign raw URL so the browser surfaces
    // a concrete error rather than a silent empty element.
    video.src = url;
  }, [url]);

  // Apply volume/mute on every render where the video element is
  // mounted. Keyed on `current?.id` too so a remount (new video) also
  // picks up the user's saved volume instead of defaulting to 1.0.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    v.volume = volume;
    v.muted = muted;
  }, [volume, muted, current?.id, url]);

  // Keyboard shortcuts — scoped to the modal being open.
  useEffect(() => {
    if (!current) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      // Don't hijack typing in input/textarea fields.
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
      switch (e.key) {
        case "Escape":
          e.preventDefault();
          close();
          break;
        case " ":
          e.preventDefault();
          togglePlay();
          break;
        case "ArrowRight":
          e.preventDefault();
          seekBy(5);
          break;
        case "ArrowLeft":
          e.preventDefault();
          seekBy(-5);
          break;
        case "ArrowUp":
          e.preventDefault();
          setVolume((v) => Math.min(1, v + 0.05));
          break;
        case "ArrowDown":
          e.preventDefault();
          setVolume((v) => Math.max(0, v - 0.05));
          break;
        case "f":
        case "F":
          e.preventDefault();
          toggleFullscreen();
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // togglePlay / seekBy / toggleFullscreen are all useCallback with
    // no deps, so they're stable — no need to list them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) v.play();
    else v.pause();
  }, []);

  const seekBy = useCallback((sec: number) => {
    const v = videoRef.current;
    if (!v) return;
    // Use Infinity as the ceiling when `v.duration` is 0 (metadata
    // not yet loaded) — otherwise `Math.min(0 || sec, ...)` caps
    // currentTime to 5, turning a forward nudge into a seek-to-5.
    const ceiling = v.duration > 0 ? v.duration : Infinity;
    v.currentTime = Math.max(0, Math.min(ceiling, v.currentTime + sec));
  }, []);

  const seekTo = useCallback((sec: number) => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = sec;
  }, []);

  const toggleFullscreen = useCallback(() => {
    // Target the whole modal — not just the <video> — so our custom
    // controls (top bar, bottom bar, volume, quality picker) stay
    // visible in fullscreen. Fullscreening just the video leaves the
    // chrome behind and also triggers WebKit's native video player
    // overlay, which was leaving white-flash artifacts on exit.
    const modal = modalRef.current;
    if (!modal) return;
    const doc = document as Document & {
      webkitFullscreenElement?: Element | null;
      webkitExitFullscreen?: () => void;
    };
    const elem = modal as HTMLDivElement & {
      webkitRequestFullscreen?: () => void;
    };
    const inFullscreen = !!(document.fullscreenElement || doc.webkitFullscreenElement);
    try {
      if (inFullscreen) {
        (document.exitFullscreen || doc.webkitExitFullscreen)?.call(document);
      } else if (elem.requestFullscreen) {
        elem.requestFullscreen();
      } else if (elem.webkitRequestFullscreen) {
        elem.webkitRequestFullscreen();
      }
    } catch {
      /* swallow — fullscreen isn't available in this host */
    }
  }, []);

  const togglePip = useCallback(async () => {
    const v = videoRef.current;
    if (!v) return;
    const doc = document as Document & {
      pictureInPictureElement?: Element | null;
      pictureInPictureEnabled?: boolean;
      exitPictureInPicture?: () => Promise<void>;
    };
    const elem = v as HTMLVideoElement & {
      webkitSupportsPresentationMode?: (mode: string) => boolean;
      webkitSetPresentationMode?: (mode: string) => void;
      webkitPresentationMode?: string;
    };
    // PiP prerequisite: the video must have loaded enough metadata
    // that the browser knows its dimensions. If not yet ready, wait
    // for `loadedmetadata` (with a 2s safety timeout). Both paths
    // must remove the listener or it leaks — a previous version
    // resolved on timeout but left the listener attached.
    if (v.readyState < HTMLMediaElement.HAVE_METADATA) {
      try {
        await new Promise<void>((resolve) => {
          const done = () => {
            clearTimeout(timer);
            v.removeEventListener("loadedmetadata", done);
            resolve();
          };
          const timer = setTimeout(() => {
            v.removeEventListener("loadedmetadata", done);
            resolve();
          }, 2000);
          v.addEventListener("loadedmetadata", done);
        });
      } catch {
        /* ignore */
      }
    }
    try {
      // Prefer the webkit presentation-mode API on macOS WKWebView —
      // it's the path WebKit itself uses for <video> PiP, and the
      // standard `requestPictureInPicture()` sometimes throws even
      // though it's defined. Fall back to the standard API for
      // Chromium hosts (WebView2 on Windows).
      if (typeof elem.webkitSupportsPresentationMode === "function") {
        if (elem.webkitSupportsPresentationMode("picture-in-picture")) {
          const mode =
            elem.webkitPresentationMode === "picture-in-picture"
              ? "inline"
              : "picture-in-picture";
          elem.webkitSetPresentationMode?.(mode);
          return;
        }
      }
      if (doc.pictureInPictureElement) {
        await doc.exitPictureInPicture?.();
        return;
      }
      if (
        doc.pictureInPictureEnabled &&
        typeof v.requestPictureInPicture === "function"
      ) {
        await v.requestPictureInPicture();
        return;
      }
      // Nothing supported — log so we can see why in devtools.
      // eslint-disable-next-line no-console
      console.warn("[video] PiP not supported in this host", {
        pipEnabled: doc.pictureInPictureEnabled,
        hasStandard: typeof v.requestPictureInPicture === "function",
        hasWebkit: typeof elem.webkitSupportsPresentationMode === "function",
      });
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("[video] PiP request failed", err);
    }
  }, []);

  // Shuffle-aware next. When shuffle is on we pick a random index from
  // the queue (not the current one) and `open` the video directly —
  // useVideoPlayer's sequential `next()` would just move to i+1.
  const advance = useCallback(() => {
    // Shuffle path — pick a random entry other than the current one.
    // Guard against the single-item queue case where `others` is
    // empty; without the guard `queue[undefined]` passes through and
    // `open()` crashes.
    if (shuffle && queue.length > 1) {
      const others = queue.map((_, i) => i).filter((i) => i !== queueIndex);
      if (others.length > 0) {
        const pickIdx = others[Math.floor(Math.random() * others.length)];
        open(queue[pickIdx], queue);
        return;
      }
    }
    if (hasNext) next();
  }, [shuffle, queue, queueIndex, hasNext, next, open]);

  if (!current) return null;

  const poster = current.cover ? imageProxy(current.cover) ?? undefined : undefined;

  // Single-branch layout — minimized vs full mode change the
  // wrapping chrome but NOT the position of the `<video>` element.
  // Keeping the video in the same JSX slot means React reuses the
  // DOM node across transitions, so minimize/expand doesn't re-load
  // the HLS or reset playback position. An earlier version rendered
  // two completely different trees; that caused a remount on every
  // toggle.
  const videoEl =
    error ? (
      <div className="text-sm text-destructive">Couldn't load video: {error}</div>
    ) : !url ? (
      <>
        {poster && (
          <img
            src={poster}
            alt=""
            className="max-h-full max-w-full object-contain opacity-70"
          />
        )}
        <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-muted-foreground">
          {loading && <Loader2 className="h-5 w-5 animate-spin" />}
          Loading…
        </div>
      </>
    ) : (
      <video
        ref={videoRef}
        key={current.id}
        poster={poster}
        autoPlay
        playsInline
        preload="auto"
        loop={repeat}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(e) =>
          setCurrentTime((e.target as HTMLVideoElement).currentTime)
        }
        onLoadedMetadata={(e) =>
          setDuration((e.target as HTMLVideoElement).duration || 0)
        }
        onEnded={() => {
          if (repeat) return;
          advance();
        }}
        className={cn(
          minimized ? "h-full w-full object-contain" : "max-h-full max-w-full",
        )}
      />
    );

  return (
    <div
      ref={modalRef}
      className={cn(
        "fixed z-[60] flex flex-col overflow-hidden bg-black text-foreground",
        minimized
          ? "bottom-4 right-4 w-[360px] rounded-lg shadow-2xl ring-1 ring-white/10"
          : "inset-0",
      )}
      onClick={(e) => {
        if (!minimized && e.target === e.currentTarget) setMinimized(true);
      }}
    >
      {!minimized && (
        <TopBar
          video={current}
          showSimilar={showSimilar}
          onToggleSimilar={() => setShowSimilar((v) => !v)}
          onShowCredits={() => setShowCredits(true)}
          onPip={togglePip}
          onFullscreen={toggleFullscreen}
          onMinimize={() => setMinimized(true)}
          onClose={close}
        />
      )}

      <div
        className={cn(
          minimized ? "relative aspect-video w-full" : "flex min-h-0 flex-1",
        )}
      >
        <div
          className={cn(
            minimized
              ? "group relative h-full w-full"
              : "relative flex flex-1 items-center justify-center",
          )}
        >
          {videoEl}
          {minimized && (
            <>
              <button
                onClick={() => setMinimized(false)}
                className="absolute left-2 top-2 flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-foreground opacity-0 transition-opacity hover:bg-black/80 group-hover:opacity-100"
                title="Expand"
                aria-label="Expand"
              >
                <Maximize2 className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={close}
                className="absolute right-2 top-2 flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-foreground opacity-0 transition-opacity hover:bg-black/80 group-hover:opacity-100"
                title="Close"
                aria-label="Close"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </div>

        {!minimized && showSimilar && (
          <SimilarVideosPanel
            videoId={current.id}
            onClose={() => setShowSimilar(false)}
          />
        )}
      </div>

      {minimized ? (
        <div className="flex items-center gap-3 border-t border-white/10 bg-black/80 px-3 py-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs font-semibold text-foreground">
              {current.name}
            </div>
            {current.artist && (
              <div className="truncate text-[11px] text-muted-foreground">
                {current.artist.name}
              </div>
            )}
          </div>
          <button
            onClick={togglePlay}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-foreground text-background"
            title={playing ? "Pause" : "Play"}
            aria-label={playing ? "Pause" : "Play"}
          >
            {playing ? (
              <Pause className="h-3.5 w-3.5" fill="currentColor" />
            ) : (
              <Play className="h-3.5 w-3.5 ml-0.5" fill="currentColor" />
            )}
          </button>
          <button
            onClick={advance}
            disabled={!(hasNext || (shuffle && queue.length > 1))}
            className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:text-foreground disabled:opacity-30"
            title="Next"
            aria-label="Next"
          >
            <SkipForward className="h-3.5 w-3.5" />
          </button>
        </div>
      ) : (
        <BottomBar
          video={current}
          playing={playing}
          currentTime={currentTime}
          duration={duration}
          shuffle={shuffle}
          repeat={repeat}
          hasNext={hasNext || (shuffle && queue.length > 1)}
          hasPrev={hasPrev}
          onTogglePlay={togglePlay}
          onNext={advance}
          onPrev={prev}
          onToggleShuffle={() => setShuffle((s) => !s)}
          onToggleRepeat={() => setRepeat((r) => !r)}
          onSeek={seekTo}
          quality={quality ?? current.quality}
          onQualityChange={setQuality}
          volume={volume}
          muted={muted}
          onVolumeChange={(v) => {
            setVolume(v);
            if (v > 0) setMuted(false);
          }}
          onToggleMute={() => setMuted((m) => !m)}
        />
      )}

      <VideoCreditsDialog
        videoId={current.id}
        videoName={current.name}
        open={showCredits}
        onOpenChange={setShowCredits}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top bar
// ---------------------------------------------------------------------------

function TopBar({
  video,
  showSimilar,
  onToggleSimilar,
  onShowCredits,
  onPip,
  onFullscreen,
  onMinimize,
  onClose,
}: {
  video: Video;
  showSimilar: boolean;
  onToggleSimilar: () => void;
  onShowCredits: () => void;
  onPip: () => void;
  onFullscreen: () => void;
  onMinimize: () => void;
  onClose: () => void;
}) {
  // PiP support across hosts: modern browsers expose
  // `document.pictureInPictureEnabled`; older WKWebView exposes
  // `webkitSupportsPresentationMode` on the video element. We
  // optimistically always show the button since the click handler
  // handles both paths and silently no-ops if neither is available —
  // hiding it entirely would mislead users who could have used it.
  return (
    <div className="flex items-center justify-between gap-4 px-6 py-3">
      <div className="min-w-0 truncate text-sm text-muted-foreground">
        {video.artist?.name}
      </div>
      <div className="flex flex-shrink-0 items-center gap-2">
        <TopBarButton
          label="Similar videos"
          icon={VideoIcon}
          active={showSimilar}
          onClick={onToggleSimilar}
          title="Similar videos"
          pill
        />
        <TopBarButton
          label="Credits"
          icon={FileText}
          onClick={onShowCredits}
          title="Credits"
          pill
        />
        <TopBarButton
          icon={PictureInPicture2}
          onClick={onPip}
          title="Picture in picture"
        />
        <TopBarButton
          icon={Maximize2}
          onClick={onFullscreen}
          title="Fullscreen (F)"
        />
        <TopBarButton icon={ChevronDown} onClick={onMinimize} title="Minimize" />
        <TopBarButton icon={X} onClick={onClose} title="Close (Esc)" />
      </div>
    </div>
  );
}

function TopBarButton({
  icon: Icon,
  label,
  onClick,
  title,
  pill,
  active,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label?: string;
  onClick: () => void;
  title: string;
  pill?: boolean;
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      className={cn(
        "flex items-center gap-1.5 text-sm font-semibold transition-colors",
        pill
          ? cn(
              "rounded-full border border-border/40 bg-white/5 px-4 py-1.5 text-foreground hover:bg-white/10",
              active && "border-primary text-primary",
            )
          : "h-9 w-9 justify-center rounded-full text-muted-foreground hover:bg-white/10 hover:text-foreground",
      )}
    >
      <Icon className="h-4 w-4" />
      {pill && label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Inline volume control for the bottom bar — mute toggle next to a
// compact horizontal slider. Matches Tidal's placement near the
// quality picker.
// ---------------------------------------------------------------------------

function InlineVolumeControl({
  volume,
  muted,
  onChange,
  onToggleMute,
}: {
  volume: number;
  muted: boolean;
  onChange: (v: number) => void;
  onToggleMute: () => void;
}) {
  const effective = muted ? 0 : volume;
  return (
    <div className="flex items-center gap-2">
      <button
        onClick={onToggleMute}
        className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground"
        title={muted ? "Unmute" : "Mute"}
        aria-label={muted ? "Unmute" : "Mute"}
      >
        {effective === 0 ? (
          <VolumeX className="h-4 w-4" />
        ) : (
          <Volume2 className="h-4 w-4" />
        )}
      </button>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={effective}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="h-1 w-20 cursor-pointer accent-primary"
        aria-label="Volume"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bottom bar — info + controls + progress
// ---------------------------------------------------------------------------

function BottomBar({
  video,
  playing,
  currentTime,
  duration,
  shuffle,
  repeat,
  hasNext,
  hasPrev,
  onTogglePlay,
  onNext,
  onPrev,
  onToggleShuffle,
  onToggleRepeat,
  onSeek,
  quality,
  onQualityChange,
  volume,
  muted,
  onVolumeChange,
  onToggleMute,
}: {
  video: Video;
  playing: boolean;
  currentTime: number;
  duration: number;
  shuffle: boolean;
  repeat: boolean;
  hasNext: boolean;
  hasPrev: boolean;
  onTogglePlay: () => void;
  onNext: () => void;
  onPrev: () => void;
  onToggleShuffle: () => void;
  onToggleRepeat: () => void;
  onSeek: (sec: number) => void;
  quality: string;
  onQualityChange: (q: string | undefined) => void;
  volume: number;
  muted: boolean;
  onVolumeChange: (v: number) => void;
  onToggleMute: () => void;
}) {
  return (
    <div className="flex flex-col gap-2 border-t border-white/10 bg-black/40 px-6 py-3">
      <div className="flex items-center gap-4">
        <VideoInfo video={video} />
        <div className="flex flex-1 items-center justify-center gap-2">
          <ControlButton
            icon={Shuffle}
            onClick={onToggleShuffle}
            title="Shuffle"
            active={shuffle}
          />
          <ControlButton
            icon={SkipBack}
            onClick={onPrev}
            title="Previous"
            disabled={!hasPrev}
          />
          <button
            onClick={onTogglePlay}
            className="flex h-11 w-11 items-center justify-center rounded-full bg-foreground text-background transition-transform hover:scale-105"
            title={playing ? "Pause (Space)" : "Play (Space)"}
            aria-label={playing ? "Pause" : "Play"}
          >
            {playing ? (
              <Pause className="h-5 w-5" fill="currentColor" />
            ) : (
              <Play className="h-5 w-5 ml-0.5" fill="currentColor" />
            )}
          </button>
          <ControlButton
            icon={SkipForward}
            onClick={onNext}
            title="Next"
            disabled={!hasNext}
          />
          <ControlButton
            icon={repeat ? Repeat1 : Repeat}
            onClick={onToggleRepeat}
            title="Repeat"
            active={repeat}
          />
        </div>
        <div className="flex w-[320px] flex-shrink-0 items-center justify-end gap-3">
          <InlineVolumeControl
            volume={volume}
            muted={muted}
            onChange={onVolumeChange}
            onToggleMute={onToggleMute}
          />
          <VideoDownloadButton videoId={video.id} />
          <QualityPicker quality={quality} onChange={onQualityChange} />
        </div>
      </div>
      <ProgressBar
        currentTime={currentTime}
        duration={duration}
        onSeek={onSeek}
      />
    </div>
  );
}

function ControlButton({
  icon: Icon,
  onClick,
  title,
  disabled,
  active,
}: {
  icon: React.ComponentType<{ className?: string }>;
  onClick: () => void;
  title: string;
  disabled?: boolean;
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={cn(
        "flex h-9 w-9 items-center justify-center rounded-full transition-colors",
        active
          ? "text-primary hover:text-primary"
          : "text-muted-foreground hover:text-foreground",
        disabled && "opacity-30",
      )}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}

function VideoInfo({ video }: { video: Video }) {
  const cover = video.cover ? imageProxy(video.cover) ?? undefined : undefined;
  return (
    <div className="flex w-[280px] min-w-0 flex-shrink-0 items-center gap-3">
      {cover && (
        <div className="h-11 w-11 flex-shrink-0 overflow-hidden rounded bg-secondary">
          <img src={cover} alt="" className="h-full w-full object-cover" />
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1 truncate text-sm font-semibold">
          <span className="truncate">{video.name}</span>
          {video.explicit && (
            <span className="flex-shrink-0 rounded-sm bg-white/10 px-1 text-[10px] font-bold">
              E
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 truncate text-xs text-muted-foreground">
          {video.artist ? (
            <Link
              to={`/artist/${video.artist.id}`}
              className="truncate hover:underline"
            >
              {video.artist.name}
            </Link>
          ) : null}
          <span className="flex-shrink-0 opacity-70">· Video</span>
        </div>
      </div>
      <VideoOverflowMenu video={video} />
    </div>
  );
}

function VideoOverflowMenu({ video }: { video: Video }) {
  const toast = useToast();
  const navigate = useNavigate();
  const { close } = useVideoPlayer();
  const shareUrl =
    video.share_url || `https://tidal.com/browse/video/${video.id}`;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      toast.show({ kind: "success", title: "Link copied" });
    } catch {
      /* ignore */
    }
  };

  const openOnTidal = async () => {
    try {
      await api.openExternal(shareUrl);
    } catch {
      window.open(shareUrl, "_blank", "noopener");
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-white/10 hover:text-foreground"
          title="More"
          aria-label="More"
        >
          <MoreHorizontal className="h-4 w-4" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="z-[65] w-52">
        <DropdownMenuItem onSelect={copy}>
          <Copy className="h-3.5 w-3.5" /> Copy link
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={openOnTidal}>
          <ExternalLink className="h-3.5 w-3.5" /> Open on Tidal
        </DropdownMenuItem>
        {video.artist && (
          <DropdownMenuItem
            onSelect={() => {
              close();
              if (video.artist) navigate(`/artist/${video.artist.id}`);
            }}
          >
            <User className="h-3.5 w-3.5" /> Go to artist
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function QualityPicker({
  quality,
  onChange,
}: {
  quality: string;
  onChange: (q: string | undefined) => void;
}) {
  const label = qualityLabel(quality) || "AUTO";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="rounded-full border border-primary/40 bg-primary/10 px-3 py-1 text-xs font-bold uppercase tracking-wider text-primary transition-colors hover:bg-primary/20"
          title="Video quality"
        >
          {label}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="z-[65] w-40">
        <DropdownMenuItem onSelect={() => onChange(undefined)}>Auto</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onChange("HIGH")}>1080p</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onChange("MEDIUM")}>720p</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onChange("LOW")}>480p</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function qualityLabel(q: string): string {
  switch (q.toUpperCase()) {
    case "HIGH":
      return "1080P";
    case "MEDIUM":
      return "720P";
    case "LOW":
      return "480P";
    case "AUDIO_ONLY":
      return "AUDIO";
    default:
      return "";
  }
}

// ---------------------------------------------------------------------------
// Progress bar
// ---------------------------------------------------------------------------

function ProgressBar({
  currentTime,
  duration,
  onSeek,
}: {
  currentTime: number;
  duration: number;
  onSeek: (sec: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const pct = duration > 0 ? (currentTime / duration) * 100 : 0;

  const handleSeek = (clientX: number) => {
    const el = trackRef.current;
    if (!el || duration <= 0) return;
    const rect = el.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    onSeek(ratio * duration);
  };

  return (
    <div className="flex items-center gap-3 text-xs tabular-nums text-muted-foreground">
      <span className="w-10 text-right">{formatDuration(currentTime)}</span>
      <div
        ref={trackRef}
        className="group relative h-1 flex-1 cursor-pointer rounded-full bg-white/10"
        onClick={(e) => handleSeek(e.clientX)}
      >
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-foreground group-hover:bg-primary"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="w-10">{formatDuration(duration)}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Similar videos side panel
// ---------------------------------------------------------------------------

function SimilarVideosPanel({
  videoId,
  onClose,
}: {
  videoId: string;
  onClose: () => void;
}) {
  const { open } = useVideoPlayer();
  const [videos, setVideos] = useState<Video[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    setVideos(null);
    api
      .videoSimilar(videoId)
      .then((rows) => {
        if (!cancelled) setVideos(rows);
      })
      .catch(() => {
        if (!cancelled) setVideos([]);
      });
    return () => {
      cancelled = true;
    };
  }, [videoId]);

  return (
    <aside className="flex w-80 flex-shrink-0 flex-col border-l border-white/10 bg-black/60">
      <div className="flex items-center justify-between px-4 py-3">
        <div className="text-sm font-semibold">Similar videos</div>
        <button
          onClick={onClose}
          className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground hover:bg-white/10 hover:text-foreground"
          aria-label="Close panel"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-4 pb-4">
        {!videos && (
          <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        )}
        {videos && videos.length === 0 && (
          <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
            <Info className="h-4 w-4" /> No similar videos.
          </div>
        )}
        {videos?.map((v) => (
          <button
            key={v.id}
            onClick={() => open(v, videos)}
            className="group flex w-full items-center gap-3 rounded-md p-2 text-left transition-colors hover:bg-white/10"
          >
            <div className="h-12 w-20 flex-shrink-0 overflow-hidden rounded bg-secondary">
              {v.cover ? (
                <img
                  src={imageProxy(v.cover) ?? ""}
                  alt=""
                  className="h-full w-full object-cover"
                  loading="lazy"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center">
                  <VideoIcon className="h-4 w-4 text-muted-foreground" />
                </div>
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-semibold">{v.name}</div>
              {v.artist && (
                <div className="truncate text-[11px] text-muted-foreground">
                  {v.artist.name}
                </div>
              )}
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Video credits dialog
// ---------------------------------------------------------------------------

function VideoCreditsDialog({
  videoId,
  videoName,
  open,
  onOpenChange,
}: {
  videoId: string;
  videoName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [credits, setCredits] = useState<CreditEntry[] | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setCredits(null);
    api
      .videoCredits(videoId)
      .then((rows) => {
        if (!cancelled) setCredits(rows);
      })
      .catch(() => {
        if (!cancelled) setCredits([]);
      });
    return () => {
      cancelled = true;
    };
  }, [videoId, open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="z-[65] max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Credits</DialogTitle>
          <DialogDescription className="truncate">{videoName}</DialogDescription>
        </DialogHeader>
        {!credits && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading credits…
          </div>
        )}
        {credits && credits.length === 0 && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Info className="h-4 w-4" /> No credits listed for this video.
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

// Share the saved volume across modal re-opens.
function readVolume(): number {
  try {
    const raw = localStorage.getItem("tidal-downloader:video-volume");
    if (!raw) return 1;
    const parsed = parseFloat(raw);
    if (isNaN(parsed) || parsed < 0 || parsed > 1) return 1;
    return parsed;
  } catch {
    return 1;
  }
}
