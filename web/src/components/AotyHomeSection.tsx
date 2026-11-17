import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Heart, Music, Star } from "lucide-react";
import type { AotyAlbum, AotyPage, Album } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { useCardColumns } from "@/hooks/useCardColumns";
import { useFavorites } from "@/hooks/useFavorites";
import { useHeartPop } from "@/components/HeartButton";
import { api } from "@/api/client";
import { Grid, ViewMoreLink } from "@/components/Grid";
import { GridSkeleton } from "@/components/Skeletons";
import { PlayMediaButton } from "@/components/PlayMediaButton";
import { cn, imageUrl } from "@/lib/utils";

/**
 * Two AOTY-backed rows on the Home page: the year's highest-user-rated
 * albums and AOTY's recently-released grid. Each row fetches its own
 * data so a slow / failed call to one doesn't block the other, and
 * empty-or-errored rows render nothing at all (the section is purely
 * additive — Home stays usable if AOTY is down).
 *
 * Each row caps to the cards that fit in one row of its own width (via
 * useCardColumns) and links to a drill-down page (/aoty/top-of-year,
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
  // Bumped when a Cloudflare solve finishes, so the rows and the status
  // fetch again and show what the solve made possible.
  const [generation, setGeneration] = useState(0);
  // Set when a row comes back `pending`.
  const [waiting, setWaiting] = useState(false);
  const onPending = useCallback(() => setWaiting(true), []);
  // Scraper status. When AOTY's Cloudflare challenge can't be cleared
  // the rows render nothing (silent-by-design); the notice below tells
  // the user that's why, and `can_solve` picks which of the two
  // reasons it explains.
  const { data: status } = useApi(() => api.aoty.status(), [generation]);
  // A solve is running: a row came back `pending`, or the status says
  // so. The rows keep their skeletons and the notice stays down until
  // it finishes.
  const solving = waiting || (status?.solving ?? false);
  useEffect(() => {
    if (!solving) return;
    let cancelled = false;
    let timer: number | undefined;
    const done = () => {
      setWaiting(false);
      setGeneration((g) => g + 1);
    };
    const check = () => {
      api.aoty.status().then(
        (s) => {
          if (cancelled) return;
          if (s.solving) timer = window.setTimeout(check, SOLVE_POLL_MS);
          else done();
        },
        () => {
          if (!cancelled) done();
        },
      );
    };
    timer = window.setTimeout(check, SOLVE_POLL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [solving]);
  return (
    <div>
      {status?.blocked && !solving && (
        <AotyBlockedNotice canSolve={status.can_solve} />
      )}
      <AotyRow
        title="New album releases"
        fetch={() => api.aoty.recentReleases({ limit: 30 })}
        viewMoreTo="/aoty/new-releases"
        generation={generation}
        onPending={onPending}
      />
      <AotyRow
        title={`Top albums of ${year}`}
        fetch={() => api.aoty.topOfYear({ limit: 30 })}
        viewMoreTo="/aoty/top-of-year"
        generation={generation}
        onPending={onPending}
      />
    </div>
  );
}

/** How often the Home rows ask whether a Cloudflare solve has finished.
 *  A solve takes roughly 5-25 s, so this keeps the wait after it short
 *  without hammering the local server. */
const SOLVE_POLL_MS = 3000;

/**
 * Shown above the AOTY rows when albumoftheyear.org is serving a
 * Cloudflare challenge we couldn't clear. Without this the rows just
 * disappear, which is what made the original outage confusing to
 * triage — same visible symptom whether the scraper is broken or AOTY
 * happens to have nothing for the week.
 *
 * Two wordings for the two causes. `canSolve` false means there's no
 * webview to run Cloudflare's challenge in — a dev server or
 * `--browser` mode, where empty rows are expected. `canSolve` true
 * means we had an engine and the solve still failed. Through most of
 * November 2026 that was AOTY asking for a human click and then
 * rejecting the embedded browser regardless, which no bug report can
 * fix, so the notice explains it rather than asking for one. A failed
 * solve is retried by the next AOTY request after a ten-minute hold,
 * so the rows come back by themselves when the check clears.
 */
function AotyBlockedNotice({ canSolve }: { canSolve: boolean }) {
  return (
    <div className="mt-8 flex items-start gap-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-4 text-sm">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
      <div className="min-w-0">
        <p className="font-semibold">Album of the Year rows unavailable</p>
        {canSolve ? (
          <p className="mt-1 text-muted-foreground">
            AlbumOfTheYear.org&apos;s check that visitors are human didn&apos;t
            let Tideway&apos;s built-in browser through. Tideway tries again in
            a few minutes, and these rows come back once it gets through.
          </p>
        ) : (
          <p className="mt-1 text-muted-foreground">
            AlbumOfTheYear.org requires a browser to clear its anti-bot
            challenge, and this build has no window to do that in. The rows work
            in the packaged desktop app.
          </p>
        )}
      </div>
    </div>
  );
}

function AotyRow({
  title,
  fetch,
  viewMoreTo,
  generation,
  onPending,
}: {
  title: string;
  fetch: () => Promise<AotyPage>;
  viewMoreTo: string;
  generation: number;
  onPending: () => void;
}) {
  const [rowRef, cols] = useCardColumns();
  const { data, loading, error } = useApi(fetch, [generation]);
  const pending = data?.pending ?? false;
  useEffect(() => {
    if (pending) onPending();
  }, [pending, onPending]);

  // Silent failure mode — Home stays usable. The user sees an empty
  // chart cycle, not a bright red error block.
  if (error) return null;
  if (loading || pending) {
    return (
      <div ref={rowRef}>
        <SectionHeader title={title} viewMoreTo={null} />
        <GridSkeleton count={cols} columns={cols} />
      </div>
    );
  }

  const playable = (data?.items ?? []).filter(
    (e): e is AotyAlbum & { tidal_album: Album } => e.tidal_album !== null,
  );
  if (playable.length === 0) return null;

  const visible = playable.slice(0, cols);
  const hasMore = playable.length > cols;

  return (
    <div ref={rowRef}>
      <SectionHeader title={title} viewMoreTo={hasMore ? viewMoreTo : null} />
      <Grid columns={cols}>
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
    <div className="mb-4 mt-8 flex items-start justify-between gap-4">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">{title}</h2>
        <AotyAttribution />
      </div>
      {viewMoreTo && <ViewMoreLink to={viewMoreTo} />}
    </div>
  );
}

/**
 * Small attribution line shown under every AOTY-sourced section
 * title. The data is scraped from albumoftheyear.org, so it's
 * credited wherever it surfaces. Exported because the drill-down
 * page shows the same credit under its page title.
 */
export function AotyAttribution() {
  return (
    <p className="mt-0.5 text-xs text-muted-foreground">
      Courtesy of{" "}
      <a
        href="https://www.albumoftheyear.org"
        target="_blank"
        rel="noreferrer"
        className="underline underline-offset-2 hover:text-foreground"
      >
        Album of the Year
      </a>
    </p>
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
  const cover = imageUrl(album.cover);
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
            className="absolute left-2 top-2 rounded bg-black/75 px-1.5 py-0.5 text-xs font-bold text-white shadow-sm"
            aria-label={`AOTY score: ${entry.score} out of 100`}
            title={`AOTY score: ${entry.score}/100`}
          >
            {entry.score}
          </div>
        )}
        {entry.must_hear && (
          <div
            className="absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full bg-amber-500 text-white shadow-sm"
            aria-label="AOTY must hear"
            title="AOTY must hear"
          >
            <Star className="h-3.5 w-3.5 fill-current" />
          </div>
        )}
        <div className="absolute bottom-2 left-2 opacity-0 transition-all duration-200 ease-out group-hover:opacity-100 focus-within:opacity-100">
          <PlayMediaButton kind="album" id={album.id} className="h-10 w-10" />
        </div>
        <AotyHeart
          albumId={album.id}
          className="absolute bottom-2 right-2 opacity-0 transition-all duration-200 ease-out group-hover:opacity-100 focus-within:opacity-100"
        />
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

/**
 * Self-contained heart button for the AOTY card overlay. Mirrors the
 * InlineHeart used inside MediaCard — the AOTY rows skip MediaCard
 * because they need the AOTY score / must-hear overlays, but they
 * still want the same hover-reveal like-toggle every other album
 * card on the app has. Stops propagation so the click doesn't follow
 * the parent Link to /album/<id>.
 */
function AotyHeart({
  albumId,
  className,
}: {
  albumId: string;
  className?: string;
}) {
  const favs = useFavorites();
  const liked = favs.has("album", albumId);
  const popping = useHeartPop(liked);
  return (
    <button
      type="button"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        void favs.toggle("album", albumId);
      }}
      aria-pressed={liked}
      aria-label={liked ? "Unlike album" : "Like album"}
      title={liked ? "Unlike album" : "Like album"}
      className={cn(
        "flex h-10 w-10 items-center justify-center rounded-full bg-black/70 text-white shadow-lg transition-colors hover:bg-black/90",
        className,
      )}
    >
      <Heart
        className={cn(
          "h-5 w-5",
          liked && "fill-primary stroke-primary",
          popping && "animate-heart-pop",
        )}
      />
    </button>
  );
}
