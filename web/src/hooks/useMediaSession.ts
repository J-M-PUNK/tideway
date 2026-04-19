import { useEffect, useRef } from "react";
import { usePlayerActions, usePlayerMeta, usePlayerTime } from "./PlayerContext";
import { imageProxy } from "@/lib/utils";

/**
 * Bridges the player into the browser's MediaSession API so the OS exposes
 * track metadata + transport controls to:
 *   - macOS Control Center / Touch Bar / lock screen
 *   - Bluetooth headphone buttons (play/pause/next/prev)
 *   - Windows Media Overlay
 *
 * Pulls from PlayerContext internally so this hook doesn't need props and
 * doesn't reinstall action handlers on every render.
 */
export function useMediaSession() {
  const { track, playing } = usePlayerMeta();
  const { currentTime, duration } = usePlayerTime();
  const actions = usePlayerActions();

  // Mirror for action handlers so they read the latest without re-installing.
  const actionsRef = useRef(actions);
  useEffect(() => {
    actionsRef.current = actions;
  }, [actions]);
  const trackRef = useRef(track);
  useEffect(() => {
    trackRef.current = track;
  }, [track]);
  const currentTimeRef = useRef(currentTime);
  useEffect(() => {
    currentTimeRef.current = currentTime;
  }, [currentTime]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    if (!track) {
      navigator.mediaSession.metadata = null;
      navigator.mediaSession.playbackState = "none";
      return;
    }
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.name,
      artist: track.artists.map((a) => a.name).join(", "),
      album: track.album?.name || "",
      artwork: artworkSet(track.album?.cover ?? null),
    });
  }, [track]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.playbackState = playing ? "playing" : track ? "paused" : "none";
  }, [playing, track]);

  // Install action handlers once. They read live state through refs.
  useEffect(() => {
    if (!("mediaSession" in navigator)) return;

    const set = (action: MediaSessionAction, handler: MediaSessionActionHandler | null) => {
      try {
        navigator.mediaSession.setActionHandler(action, handler);
      } catch {
        /* not supported — ignore */
      }
    };

    set("play", () => trackRef.current && actionsRef.current.toggle());
    set("pause", () => trackRef.current && actionsRef.current.toggle());
    set("nexttrack", () => actionsRef.current.next());
    set("previoustrack", () => actionsRef.current.prev());
    set("seekto", (details) => {
      if (typeof details.seekTime === "number") actionsRef.current.seek(details.seekTime);
    });
    set("seekforward", (details) => {
      const offset = details.seekOffset ?? 10;
      actionsRef.current.seek(currentTimeRef.current + offset);
    });
    set("seekbackward", (details) => {
      const offset = details.seekOffset ?? 10;
      actionsRef.current.seek(currentTimeRef.current - offset);
    });

    return () => {
      (
        [
          "play",
          "pause",
          "nexttrack",
          "previoustrack",
          "seekto",
          "seekforward",
          "seekbackward",
        ] as MediaSessionAction[]
      ).forEach((a) => set(a, null));
    };
  }, []);

  // Keep the OS scrubber roughly in sync. Floor to integer seconds so we
  // only fire once per real clock second, not on every 250ms timeupdate.
  const wholeSecond = Math.floor(currentTime);
  useEffect(() => {
    if (!("mediaSession" in navigator) || !navigator.mediaSession.setPositionState) return;
    if (!track || !duration) return;
    try {
      navigator.mediaSession.setPositionState({
        duration,
        position: Math.min(wholeSecond, duration),
        playbackRate: 1,
      });
    } catch {
      /* ignore — some browsers reject zero-duration states */
    }
  }, [track, duration, wholeSecond]);
}

function artworkSet(cover: string | null): MediaImage[] {
  if (!cover) return [];
  const url = imageProxy(cover);
  if (!url) return [];
  return [
    { src: url, sizes: "320x320", type: "image/jpeg" },
    { src: url, sizes: "640x640", type: "image/jpeg" },
  ];
}
