import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import type { Video } from "@/api/types";

/**
 * Global controller for the music-video modal. The state carries both
 * the currently-playing video AND the queue it was opened from — so
 * when the user hits next/prev in the video modal we flip through the
 * artist's other videos instead of just closing. Keeping this separate
 * from the audio player means the audio context stays audio-only, and
 * we pause its playback while video is showing.
 */
interface VideoPlayerState {
  current: Video | null;
  queue: Video[];
  queueIndex: number;
  /** Open a video. If a queue is provided, next/prev navigate through
   *  it; the current video doesn't need to be *in* the queue (though
   *  callers usually pass the list the video was clicked from). */
  open: (video: Video, queue?: Video[]) => void;
  close: () => void;
  next: () => void;
  prev: () => void;
  hasNext: boolean;
  hasPrev: boolean;
}

const Ctx = createContext<VideoPlayerState | null>(null);

export function VideoPlayerProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<Video | null>(null);
  const [queue, setQueue] = useState<Video[]>([]);
  const [queueIndex, setQueueIndex] = useState(-1);

  const open = useCallback((video: Video, queueArg?: Video[]) => {
    const effectiveQueue = queueArg && queueArg.length > 0 ? queueArg : [video];
    const idx = effectiveQueue.findIndex((v) => v.id === video.id);
    setQueue(effectiveQueue);
    setQueueIndex(idx >= 0 ? idx : 0);
    setCurrent(video);
  }, []);

  const close = useCallback(() => {
    setCurrent(null);
    setQueue([]);
    setQueueIndex(-1);
  }, []);

  const next = useCallback(() => {
    setQueueIndex((i) => {
      if (i < 0 || queue.length === 0) return i;
      const nextIdx = i + 1;
      if (nextIdx >= queue.length) return i;
      setCurrent(queue[nextIdx]);
      return nextIdx;
    });
  }, [queue]);

  const prev = useCallback(() => {
    setQueueIndex((i) => {
      if (i <= 0 || queue.length === 0) return i;
      const prevIdx = i - 1;
      setCurrent(queue[prevIdx]);
      return prevIdx;
    });
  }, [queue]);

  const value = useMemo<VideoPlayerState>(
    () => ({
      current,
      queue,
      queueIndex,
      open,
      close,
      next,
      prev,
      hasNext: queueIndex >= 0 && queueIndex < queue.length - 1,
      hasPrev: queueIndex > 0,
    }),
    [current, queue, queueIndex, open, close, next, prev],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useVideoPlayer(): VideoPlayerState {
  const v = useContext(Ctx);
  if (!v)
    throw new Error("useVideoPlayer must be used inside VideoPlayerProvider");
  return v;
}
