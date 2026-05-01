import { Link } from "react-router-dom";
import { Music, Star } from "lucide-react";
import type { AotyAlbum, Album } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { api } from "@/api/client";
import { SectionHeader } from "@/components/Grid";
import { GridSkeleton } from "@/components/Skeletons";
import { PlayMediaButton } from "@/components/PlayMediaButton";
import { imageProxy } from "@/lib/utils";

/**
 * Two AOTY-backed rows on the Home page: the year's highest-user-rated
 * albums and AOTY's recently-released grid. Each row fetches its own
 * data so a slow / failed call to one doesn't block the other, and
 * empty-or-errored rows render nothing at all (the section is purely
 * additive — Home stays usable if AOTY is down).
 *
 * Entries without a resolved Tidal album are filtered out: the goal is
 * a discovery surface the user can play from, not a chart for its own
 * sake. AOTY's own page is still one click away via the section
 * subtitle on each entry.
 */
export function AotyHomeSection({ onDownload }: { onDownload: OnDownload }) {
  const year = new Date().getFullYear();
  return (
    <div>
      <AotyRow
        title={`Top albums of ${year}`}
        fetch={() => api.aoty.topOfYear({ limit: 30 })}
        onDownload={onDownload}
      />
      <AotyRow
        title="New releases"
        fetch={() => api.aoty.recentReleases(24)}
        onDownload={onDownload}
      />
    </div>
  );
}

function AotyRow({
  title,
  fetch,
  onDownload,
}: {
  title: string;
  fetch: () => Promise<AotyAlbum[]>;
  onDownload: OnDownload;
}) {
  const { data, loading, error } = useApi(fetch, []);

  // Silent failure mode — Home stays usable. The user sees an empty
  // chart cycle, not a bright red error block.
  if (error) return null;
  if (loading) {
    return (
      <div className="mb-2">
        <SectionHeader title={title} />
        <GridSkeleton count={6} />
      </div>
    );
  }

  const playable = (data ?? []).filter(
    (e): e is AotyAlbum & { tidal_album: Album } => e.tidal_album !== null,
  );
  if (playable.length === 0) return null;

  return (
    <div className="mb-2">
      <SectionHeader title={title} />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
        {playable.map((entry) => (
          <AotyCard
            key={entry.tidal_album.id}
            entry={entry}
            onDownload={onDownload}
          />
        ))}
      </div>
    </div>
  );
}

/**
 * One AOTY-decorated album card. Visually a Tidal album card — cover,
 * title, artist — with two small overlays: the AOTY score (top-left)
 * when present, and a "must hear" star (top-right) when AOTY has
 * tagged the album that way.
 */
function AotyCard({
  entry,
  onDownload: _onDownload,
}: {
  entry: AotyAlbum & { tidal_album: Album };
  onDownload: OnDownload;
}) {
  const album = entry.tidal_album;
  const cover = imageProxy(album.cover);
  const artist = album.artists.map((a) => a.name).join(", ");

  return (
    <Link
      to={`/album/${album.id}`}
      className="group relative flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors duration-200 ease-out hover:bg-accent"
    >
      <div className="relative aspect-square w-full overflow-hidden rounded-md bg-secondary">
        {cover ? (
          <img
            src={cover}
            alt={album.name}
            loading="lazy"
            className="h-full w-full object-cover transition-transform duration-300 ease-out group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-10 w-10" />
          </div>
        )}
        {entry.score !== null && (
          <div
            className="absolute left-2 top-2 rounded bg-black/75 px-1.5 py-0.5 text-xs font-bold text-white shadow"
            title={`AOTY score: ${entry.score}/100`}
          >
            {entry.score}
          </div>
        )}
        {entry.must_hear && (
          <div
            className="absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full bg-amber-500 text-white shadow"
            title="AOTY must hear"
          >
            <Star className="h-3.5 w-3.5 fill-current" />
          </div>
        )}
        <div className="absolute bottom-2 left-2 opacity-0 transition-all duration-200 ease-out group-hover:opacity-100 focus-within:opacity-100">
          <PlayMediaButton kind="album" id={album.id} className="h-10 w-10" />
        </div>
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{album.name}</div>
        <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
          {artist}
          {entry.rating_count !== null && (
            <span> · {entry.rating_count.toLocaleString()} ratings</span>
          )}
        </div>
      </div>
    </Link>
  );
}
