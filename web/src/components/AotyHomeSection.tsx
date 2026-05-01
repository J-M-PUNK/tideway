import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Music, Star } from "lucide-react";
import type { AotyAlbum, Album } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { useColumnCount } from "@/hooks/useColumnCount";
import { api } from "@/api/client";
import { Grid, ViewMoreLink } from "@/components/Grid";
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
 * Each row caps to one row of cards at the current viewport width (via
 * useColumnCount) and links to a drill-down page (/aoty/top-of-year,
 * /aoty/new-releases) for the full grid. Same pattern the rest of the
 * app uses for "Albums" / "EPs & Singles" / "Appears on" rows on the
 * artist page.
 *
 * Entries without a resolved Tidal album are filtered out: the goal is
 * a discovery surface the user can play from, not a chart for its own
 * sake.
 */
export function AotyHomeSection() {
  const year = useMemo(() => new Date().getFullYear(), []);
  return (
    <div>
      <AotyRow
        title={`Top albums of ${year}`}
        fetch={() => api.aoty.topOfYear({ limit: 30 })}
        viewMoreTo="/aoty/top-of-year"
      />
      <AotyRow
        title="New releases"
        fetch={() => api.aoty.recentReleases(24)}
        viewMoreTo="/aoty/new-releases"
      />
    </div>
  );
}

function AotyRow({
  title,
  fetch,
  viewMoreTo,
}: {
  title: string;
  fetch: () => Promise<AotyAlbum[]>;
  viewMoreTo: string;
}) {
  const cols = useColumnCount();
  const { data, loading, error } = useApi(fetch, []);

  // Silent failure mode — Home stays usable. The user sees an empty
  // chart cycle, not a bright red error block.
  if (error) return null;
  if (loading) {
    return (
      <div>
        <SectionHeader title={title} viewMoreTo={null} />
        <GridSkeleton count={cols} />
      </div>
    );
  }

  const playable = (data ?? []).filter(
    (e): e is AotyAlbum & { tidal_album: Album } => e.tidal_album !== null,
  );
  if (playable.length === 0) return null;

  const visible = playable.slice(0, cols);
  const hasMore = playable.length > cols;

  return (
    <div>
      <SectionHeader title={title} viewMoreTo={hasMore ? viewMoreTo : null} />
      <Grid>
        {visible.map((entry) => (
          <AotyCard key={entry.tidal_album.id} entry={entry} />
        ))}
      </Grid>
    </div>
  );
}

/**
 * Section header that mirrors the rest of the app's row headers
 * (artist page Albums / EPs / Appears on). h2 + optional view-more
 * link on the right. Inlined here rather than reusing the Grid
 * package's SectionHeader because the latter wraps content and we
 * need just the header strip — same visual, cleaner composition.
 */
function SectionHeader({
  title,
  viewMoreTo,
}: {
  title: string;
  viewMoreTo: string | null;
}) {
  return (
    <div className="mb-4 mt-8 flex items-baseline justify-between gap-4">
      <h2 className="text-2xl font-bold tracking-tight">{title}</h2>
      {viewMoreTo && <ViewMoreLink to={viewMoreTo} />}
    </div>
  );
}

/**
 * One AOTY-decorated album card. Visually a Tidal album card — cover,
 * title, artist — with two small overlays: the AOTY score (top-left)
 * when present, and a "must hear" star (top-right) when AOTY has
 * tagged the album that way.
 *
 * Exported because the drill-down page reuses the same card layout
 * for the full grid view.
 */
export function AotyCard({
  entry,
}: {
  entry: AotyAlbum & { tidal_album: Album };
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
            aria-label={`AOTY score: ${entry.score} out of 100`}
            title={`AOTY score: ${entry.score}/100`}
          >
            {entry.score}
          </div>
        )}
        {entry.must_hear && (
          <div
            className="absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full bg-amber-500 text-white shadow"
            aria-label="AOTY must hear"
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
