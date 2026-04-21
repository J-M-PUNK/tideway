import { useEffect, useState } from "react";
import { api } from "@/api/client";

// Last.fm's image API is effectively broken — they stopped serving
// artist images in 2019 over licensing, and album/track images come
// back as the `2a96cbd8…` grey-star placeholder for most rows. The
// backend already filters that placeholder to an empty string, so when
// `item.image` is empty the frontend falls through to this hook: look
// the thing up on Tidal by name and use Tidal's art instead.
//
// A module-level cache dedupes lookups across components (the same
// artist can appear on the Stats page, Popular page, and a history row
// in the same session).

type Kind = "artist" | "album" | "track";

type Entry = {
  url: string | null;
  promise?: Promise<string | null>;
};

const cache = new Map<string, Entry>();
const subs = new Map<string, Set<() => void>>();

function keyOf(kind: Kind, name: string, artist?: string): string {
  return `${kind}:${name.toLowerCase().trim()}:${(artist ?? "").toLowerCase().trim()}`;
}

function notify(key: string) {
  const set = subs.get(key);
  if (!set) return;
  for (const fn of set) fn();
}

async function resolve(kind: Kind, name: string, artist?: string): Promise<string | null> {
  const query = artist ? `${artist} ${name}` : name;
  try {
    const res = await api.search(query, 5);
    if (kind === "artist") {
      const exact = res.artists.find((a) => a.name.toLowerCase() === name.toLowerCase());
      return (exact ?? res.artists[0])?.picture ?? null;
    }
    if (kind === "album") {
      const exact = res.albums.find(
        (a) =>
          a.name.toLowerCase() === name.toLowerCase() &&
          (!artist || a.artists.some((ar) => ar.name.toLowerCase() === artist.toLowerCase())),
      );
      return (exact ?? res.albums[0])?.cover ?? null;
    }
    // track — prefer Tidal's album cover for the matching track
    const exact = res.tracks.find(
      (t) =>
        t.name.toLowerCase() === name.toLowerCase() &&
        (!artist || t.artists.some((ar) => ar.name.toLowerCase() === artist.toLowerCase())),
    );
    return (exact ?? res.tracks[0])?.album?.cover ?? null;
  } catch {
    return null;
  }
}

/**
 * Look up Tidal artwork for a name (+ optional artist). Returns a raw
 * Tidal URL that the caller still needs to pass through `imageProxy`,
 * matching how Tidal URLs are handled everywhere else. Returns `null`
 * while loading or when no match exists — the caller keeps its
 * existing fallback icon.
 */
export function useTidalArt(kind: Kind, name: string, artist?: string): string | null {
  const key = keyOf(kind, name, artist);
  const [, tick] = useState(0);

  useEffect(() => {
    if (!name.trim()) return;
    let active = true;
    const sub = () => {
      if (active) tick((n) => n + 1);
    };
    let set = subs.get(key);
    if (!set) {
      set = new Set();
      subs.set(key, set);
    }
    set.add(sub);

    const entry = cache.get(key);
    if (!entry) {
      const promise = resolve(kind, name, artist).then((url) => {
        cache.set(key, { url });
        notify(key);
        return url;
      });
      cache.set(key, { url: null, promise });
    }

    return () => {
      active = false;
      set?.delete(sub);
    };
  }, [key, kind, name, artist]);

  return cache.get(key)?.url ?? null;
}
