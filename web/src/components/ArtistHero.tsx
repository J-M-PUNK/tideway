import { useState } from "react";
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
            <ArtistRadioButton artistId={artistId} />
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

function ArtistRadioButton({ artistId }: { artistId: string }) {
  const [loading, setLoading] = useState(false);
  const toast = useToast();
  const actions = usePlayerActions();

  const onRadio = async () => {
    if (loading) return;
    setLoading(true);
    try {
      const tracks = await api.artistRadio(artistId);
      if (tracks.length === 0) {
        toast.show({ kind: "info", title: "No radio available" });
        return;
      }
      actions.play(tracks[0], tracks);
      toast.show({
        kind: "success",
        title: "Artist radio started",
        description: `${tracks.length} tracks queued.`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start radio",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <button
      onClick={onRadio}
      disabled={loading}
      className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
      title="Start artist radio"
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

  const openInTidal = () => window.open(shareUrl, "_blank", "noopener");

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
