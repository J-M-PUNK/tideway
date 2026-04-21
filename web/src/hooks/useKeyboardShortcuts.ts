import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { usePlayerActions, usePlayerMeta } from "./PlayerContext";
import { useFavorites } from "./useFavorites";

function isTextInput(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * Global keyboard shortcuts. All player shortcuts are no-ops when a
 * text input is focused (so typing into search / filter fields
 * doesn't eat the user's keystrokes). Cmd/Ctrl-prefixed shortcuts
 * are honored regardless of focus.
 *
 *   Cmd/Ctrl+K       — command palette / search
 *   Cmd/Ctrl+,       — Settings (macOS convention, respected on Win/Linux)
 *   Space            — play / pause
 *   Shift+←  Shift+→ — previous / next track
 *   ↑    ↓           — volume +/- 5%
 *   M                — mute / unmute
 *   S                — toggle shuffle
 *   R                — cycle repeat (off → all → one → off)
 *   L                — like / unlike the current track
 *
 * Reads player state + favorites through context so this hook
 * doesn't need to take the full player as a prop and re-register
 * handlers on every render.
 */
export function useKeyboardShortcuts({ onOpenPalette }: { onOpenPalette: () => void }) {
  const actions = usePlayerActions();
  const { track, volume } = usePlayerMeta();
  const favs = useFavorites();
  const navigate = useNavigate();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const inText = isTextInput(e.target);

      // Modifier-prefixed shortcuts fire regardless of focus —
      // browsers already reserve Cmd/Ctrl+K from the address bar.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenPalette();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === ",") {
        e.preventDefault();
        navigate("/settings");
        return;
      }

      if (inText) return;

      // --- player transport -------------------------------------------------
      if (e.key === " " || e.code === "Space") {
        if (!track) return;
        e.preventDefault();
        actions.toggle();
        return;
      }
      if (e.shiftKey && e.key === "ArrowRight") {
        e.preventDefault();
        actions.next();
        return;
      }
      if (e.shiftKey && e.key === "ArrowLeft") {
        e.preventDefault();
        actions.prev();
        return;
      }

      // --- volume -----------------------------------------------------------
      // Bare ↑/↓ — only when no modifier, so Shift/Alt/etc. combos
      // (used by browsers, text editing, etc.) aren't intercepted.
      if (
        e.key === "ArrowUp" &&
        !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey
      ) {
        e.preventDefault();
        actions.setVolume(Math.min(1, volume + 0.05));
        return;
      }
      if (
        e.key === "ArrowDown" &&
        !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey
      ) {
        e.preventDefault();
        actions.setVolume(Math.max(0, volume - 0.05));
        return;
      }

      // --- single-letter modes ---------------------------------------------
      // Guard against modifier combos — Cmd+M minimizes on macOS;
      // Cmd+R reloads; Cmd+S saves a page. Bare keys only.
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      const k = e.key.toLowerCase();
      if (k === "m") {
        actions.setVolume(volume === 0 ? 1 : 0);
      } else if (k === "s") {
        actions.toggleShuffle();
      } else if (k === "r") {
        actions.cycleRepeat();
      } else if (k === "l") {
        if (track) favs.toggle("track", track.id);
      }
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [actions, track, volume, favs, navigate, onOpenPalette]);
}
