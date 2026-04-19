import { useEffect } from "react";
import { usePlayerActions, usePlayerMeta } from "./PlayerContext";

function isTextInput(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * Global keyboard shortcuts:
 *  - Cmd/Ctrl+K → open command palette
 *  - Space → play/pause (unless focused in a text input or slider)
 *  - ← / → with Shift → prev/next track
 *  - M → mute/unmute
 *
 * Reads player state/actions through context so this hook doesn't need to
 * take the full player as a prop and re-register handlers on every render.
 */
export function useKeyboardShortcuts({ onOpenPalette }: { onOpenPalette: () => void }) {
  const actions = usePlayerActions();
  const { track, volume } = usePlayerMeta();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const inText = isTextInput(e.target);

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenPalette();
        return;
      }

      if (inText) return;

      if (e.key === " " || e.code === "Space") {
        if (!track) return;
        e.preventDefault();
        actions.toggle();
      } else if (e.shiftKey && e.key === "ArrowRight") {
        e.preventDefault();
        actions.next();
      } else if (e.shiftKey && e.key === "ArrowLeft") {
        e.preventDefault();
        actions.prev();
      } else if (e.key.toLowerCase() === "m" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        // Guard against Cmd/Ctrl+M collisions — on macOS Cmd+M minimizes
        // the window and triggering mute at the same time is surprising.
        actions.setVolume(volume === 0 ? 1 : 0);
      }
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [actions, track, volume, onOpenPalette]);
}
