import type { ComponentType } from "react";
import { Play, Video as VideoIcon } from "lucide-react";
import type { Video } from "@/api/types";
import { prefetchVideoStream } from "@/hooks/useVideoStream";
import { formatDuration, imageProxy } from "@/lib/utils";

/**
 * Thumbnail card for a music video. Shared by the artist page (single
 * row preview) and the artist-section drill-down page (full grid).
 * Plays via the parent's `onPlay` so the parent decides whether to
 * pop the modal, navigate, or queue the video into a per-section
 * playlist.
 */
export function VideoCard({
  video,
  onPlay,
  icon = VideoIcon,
}: {
  video: Video;
  onPlay: () => void;
  icon?: ComponentType<{ className?: string }>;
}) {
  const cover = video.cover ? imageProxy(video.cover) : undefined;
  const PlaceholderIcon = icon;
  return (
    <button
      onClick={onPlay}
      onMouseEnter={() => prefetchVideoStream(video.id)}
      onFocus={() => prefetchVideoStream(video.id)}
      className="group flex flex-col gap-2 rounded-lg p-2 text-left transition-colors hover:bg-accent"
    >
      <div className="relative aspect-video overflow-hidden rounded-md bg-secondary">
        {cover ? (
          <img
            src={cover}
            alt=""
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <PlaceholderIcon className="h-8 w-8" />
          </div>
        )}
        <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
          <Play className="h-8 w-8 text-foreground" fill="currentColor" />
        </span>
        {video.duration > 0 && (
          <span className="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold text-foreground">
            {formatDuration(video.duration)}
          </span>
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold">{video.name}</div>
        {video.artist && (
          <div className="truncate text-xs text-muted-foreground">{video.artist.name}</div>
        )}
      </div>
    </button>
  );
}
