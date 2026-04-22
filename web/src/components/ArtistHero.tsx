import { useNavigate } from "react-router-dom";
import {
  Check,
  Copy,
  ExternalLink,
  Heart,
  MoreHorizontal,
  Pause,
  Play,
  Radio,
  Share2,
  Shuffle,
  Download as DownloadIcon,
} from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import type { Album, Track } from "@/api/types";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useToast } from "@/components/toast";
import { useFavorites } from "@/hooks/useFavorites";
import { useLastfmArtistPlaycount } from "@/hooks/useLastfmPlaycount";
import { useSpotifyArtistStats } from "@/hooks/useSpotifyEnrichment";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { cn, imageProxy } from "@/lib/utils";

interface Props {
  artistId: string;
  artistName: string;
  picture: string | null;
  topTracks: Track[];
  allAlbums: Album[];
  shareUrl: string;
  onDownload: OnDownload;
  /** Tidal's ARTIST_MIX id for this artist. When present, the "Artist
   *  radio" button routes straight to /mix/:id — that's Tidal's
   *  canonical artist-radio page with composite cover + metadata. */
  artistMixId?: string | null;
}

/**
 * Full-width banner-style hero for the artist page, mirroring Tidal's
 * own layout: the artist photo fills the background, a dark gradient
 * keeps the title readable, and a row of actions sits below the name.
 *
 * Replaces the generic DetailHero on ArtistDetail only — album and
 * playlist heroes still use the cover-on-the-left layout since those
 * typically have a square cover (not a wide press photo).
 */
export function ArtistHero({
  artistId,
  artistName,
  picture,
  topTracks,
  allAlbums,
  shareUrl,
  onDownload,
  artistMixId,
}: Props) {
  const cover = imageProxy(picture);
  const { track, playing } = usePlayerMeta();
  const actions = usePlayerActions();
  const isOurQueue = !!track && topTracks.some((t) => t.id === track.id);
  const isPlaying = isOurQueue && playing;

  const onPlay = () => {
    if (isOurQueue) {
      actions.toggle();
      return;
    }
    if (topTracks.length === 0) return;
    actions.play(topTracks[0], topTracks);
  };

  return (
    <div className="relative -mx-8 -mt-6 mb-8 overflow-hidden">
      {/* Banner image — blurred, scaled up, and darkened so the
          foreground text is legible regardless of what the cover is. */}
      <div className="relative h-[340px] w-full">
        {cover ? (
          <img
            src={cover}
            alt=""
            className="absolute inset-0 h-full w-full scale-110 object-cover blur-xl brightness-[0.55]"
          />
        ) : (
          <div className="absolute inset-0 bg-gradient-to-b from-[#2a2a2a] to-[#0a0a0a]" />
        )}
        {cover && (
          <img
            src={cover}
            alt={artistName}
            className="absolute inset-0 h-full w-full object-cover"
            style={{
              maskImage:
                "linear-gradient(90deg, transparent 0%, black 30%, black 70%, transparent 100%)",
              WebkitMaskImage:
                "linear-gradient(90deg, transparent 0%, black 30%, black 70%, transparent 100%)",
            }}
          />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-background via-background/40 to-transparent" />
      </div>

      {/* Foreground: name + action row, positioned over the bottom of
          the banner. */}
      <div className="absolute inset-x-0 bottom-0 px-8 pb-6">
        <h1 className="text-5xl font-black tracking-tight drop-shadow-lg">
          {artistName}
        </h1>
        <ArtistPlaycountLine
          artistName={artistName}
          artistId={artistId}
          sampleIsrc={topTracks.find((t) => t.isrc)?.isrc ?? null}
        />

        <div className="mt-6 flex flex-wrap items-center gap-4">
          <button
            onClick={onPlay}
            disabled={topTracks.length === 0}
            className="flex items-center gap-2 rounded-full bg-foreground px-8 py-3 text-sm font-bold text-background shadow-xl transition-transform hover:scale-105 active:scale-95 disabled:opacity-40"
          >
            {isPlaying ? (
              <Pause className="h-4 w-4" fill="currentColor" />
            ) : (
              <Play className="h-4 w-4" fill="currentColor" />
            )}
            {isPlaying ? "Pause" : "Play"}
          </button>

          <ShuffleButton topTracks={topTracks} />

          <div className="flex flex-1 items-center justify-end gap-6">
            <FollowToggle artistId={artistId} />
            <ArtistRadioButton artistId={artistId} mixId={artistMixId ?? null} />
            <ShareButton shareUrl={shareUrl} />
            <ArtistMoreMenu
              artistId={artistId}
              artistName={artistName}
              shareUrl={shareUrl}
              allAlbums={allAlbums}
              onDownload={onDownload}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function ShuffleButton({ topTracks }: { topTracks: Track[] }) {
  const actions = usePlayerActions();
  const { shuffle } = usePlayerMeta();

  const onShuffle = () => {
    if (topTracks.length === 0) return;
    // Make sure shuffle is enabled so Next continues picking random
    // tracks from the list, then start with a random one.
    if (!shuffle) actions.toggleShuffle();
    const start = topTracks[Math.floor(Math.random() * topTracks.length)];
    actions.play(start, topTracks);
  };

  return (
    <button
      onClick={onShuffle}
      disabled={topTracks.length === 0}
      className="flex items-center gap-2 rounded-full border border-border/60 bg-black/30 px-6 py-3 text-sm font-bold text-foreground transition-colors hover:bg-black/50 disabled:opacity-40"
    >
      <Shuffle className="h-4 w-4" /> Shuffle
    </button>
  );
}

function FollowToggle({ artistId }: { artistId: string }) {
  const { has, toggle } = useFavorites();
  const following = has("artist", artistId);
  return (
    <button
      onClick={() => toggle("artist", artistId)}
      className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground"
      title={following ? "Unfollow" : "Follow"}
    >
      <div className={cn("flex h-5 items-center", following && "text-primary")}>
        {following ? <Check className="h-5 w-5" /> : <Heart className="h-5 w-5" />}
      </div>
      <div className={cn("text-xs font-semibold", following && "text-primary")}>
        {following ? "Following" : "Follow"}
      </div>
    </button>
  );
}

function ArtistRadioButton({
  artistId,
  mixId,
}: {
  artistId: string;
  mixId: string | null;
}) {
  const navigate = useNavigate();
  // Prefer Tidal's canonical mix page when we have a mix id — it
  // ships with the composite cover art, "Artist Radio" subtitle,
  // and any other entities Tidal decorates its mixes with. Fall back
  // to our generic radio page for the rare artist without a mix.
  const target = mixId
    ? `/mix/${encodeURIComponent(mixId)}`
    : `/radio/artist/${artistId}`;
  return (
    <button
      onClick={() => navigate(target)}
      className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground"
      title="Open artist radio"
    >
      <Radio className="h-5 w-5" />
      <div className="text-xs font-semibold">Artist radio</div>
    </button>
  );
}

function ShareButton({ shareUrl }: { shareUrl: string }) {
  const toast = useToast();
  const onShare = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      toast.show({ kind: "success", title: "Link copied" });
    } catch {
      toast.show({
        kind: "error",
        title: "Couldn't copy link",
        description: "Your browser blocked clipboard access.",
      });
    }
  };
  return (
    <button
      onClick={onShare}
      className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground"
      title="Copy link to artist"
    >
      <Share2 className="h-5 w-5" />
      <div className="text-xs font-semibold">Share</div>
    </button>
  );
}

function ArtistMoreMenu({
  artistName,
  shareUrl,
  allAlbums,
}: {
  artistId: string;
  artistName: string;
  shareUrl: string;
  allAlbums: Album[];
  onDownload: OnDownload;
}) {
  const toast = useToast();

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      toast.show({ kind: "success", title: "Link copied" });
    } catch {
      /* ignore */
    }
  };

  const openInTidal = async () => {
    // Route through the backend so pywebview's embedded WebView can't
    // swallow the window.open call — Python's webbrowser launches the
    // system default. Fallback keeps dev browser mode working.
    try {
      await api.openExternal(shareUrl);
    } catch {
      window.open(shareUrl, "_blank", "noopener");
    }
  };

  const downloadCatalog = async () => {
    if (allAlbums.length === 0) {
      toast.show({ kind: "info", title: "Nothing to download" });
      return;
    }
    try {
      const res = await api.downloads.enqueueBulk(
        allAlbums.map((a) => ({ kind: "album" as const, id: a.id })),
      );
      toast.show({
        kind: "success",
        title: `Queueing ${res.submitted} albums`,
        description: `${artistName}'s discography running in the background.`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't queue catalog",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground"
          title="More"
        >
          <MoreHorizontal className="h-5 w-5" />
          <div className="text-xs font-semibold">More</div>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem onSelect={copy}>
          <Copy className="h-4 w-4" /> Copy artist link
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={openInTidal}>
          <ExternalLink className="h-4 w-4" /> Open in Tidal
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={downloadCatalog}>
          <DownloadIcon className="h-4 w-4" /> Download full discography
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Last.fm context line under the artist name. Two halves, either of
 * which may be missing:
 *  - Global listeners / scrobbles across all Last.fm users. Shown
 *    whenever Last.fm has credentials configured, connected or not.
 *  - Personal "you've played them X times", shown only when the
 *    connected user has actually scrobbled them.
 * Suppresses itself entirely if neither half has data.
 */
function ArtistPlaycountLine({
  artistName,
  artistId,
  sampleIsrc,
}: {
  artistName: string;
  artistId: string;
  sampleIsrc: string | null;
}) {
  const pc = useLastfmArtistPlaycount(artistName);
  // Spotify's monthly-listener count complements Last.fm's user-
  // scrobble metrics. We need a track ISRC by this artist to pivot
  // Tidal → Spotify; if none of the top tracks carry one, skip the
  // Spotify branch and just show Last.fm data.
  const spotify = useSpotifyArtistStats(artistId, sampleIsrc);
  const monthly = spotify?.monthly_listeners ?? 0;

  const user = pc?.userplaycount ?? 0;
  const listeners = pc?.listeners ?? 0;
  const global = pc?.playcount ?? 0;

  if (user <= 0 && listeners <= 0 && global <= 0 && monthly <= 0) {
    return null;
  }

  const parts: string[] = [];
  if (monthly > 0) parts.push(`${formatCompact(monthly)} monthly listeners`);
  if (listeners > 0) parts.push(`${formatCompact(listeners)} listeners`);
  if (global > 0) parts.push(`${formatCompact(global)} plays`);
  const personal =
    user > 0
      ? `You've played them ${user.toLocaleString()} ${
          user === 1 ? "time" : "times"
        }`
      : "";

  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground drop-shadow">
      {parts.length > 0 && <span>{parts.join(" · ")}</span>}
      {parts.length > 0 && personal && <span aria-hidden>·</span>}
      {personal && <span className="text-primary">{personal}</span>}
    </div>
  );
}

/** Compact number format: 1240 → "1.2K", 1_234_567 → "1.2M". Matches
 *  what Spotify / Last.fm show. Falls back to toLocaleString for values
 *  under a thousand. */
function formatCompact(n: number): string {
  if (n < 1000) return n.toLocaleString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}
