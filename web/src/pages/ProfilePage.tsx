import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { User as UserIcon } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import type { Playlist } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { useAuth } from "@/hooks/useAuth";
import { DetailHero } from "@/components/DetailHero";
import { FollowButton } from "@/components/FollowButton";
import { Grid, SectionHeader } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { ErrorView } from "@/components/ErrorView";
import { HeroSkeleton, GridSkeleton } from "@/components/Skeletons";

/**
 * Public profile page for another Tidal user — their avatar + display
 * name in the hero, a Follow button, their public playlists as a
 * grid, and links to their followers/following lists.
 *
 * Tidal doesn't expose other users' favorited tracks / albums /
 * artists at all, so those sections that exist on `/library` aren't
 * possible here.
 */
export function ProfilePage({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const {
    data: user,
    loading,
    error,
  } = useApi(() => api.user.profile(id), [id]);
  const [playlists, setPlaylists] = useState<Playlist[] | null>(null);
  const [counts, setCounts] = useState<{
    followers: number;
    following: number;
  } | null>(null);
  const auth = useAuth();

  useEffect(() => {
    let cancelled = false;
    setPlaylists(null);
    setCounts(null);
    api.user
      .playlists(id)
      .then((rows) => !cancelled && setPlaylists(rows))
      .catch(() => !cancelled && setPlaylists([]));
    // Cheap counts endpoint — reads `totalNumberOfItems` off the
    // first page of each list instead of fetching and materializing
    // both full user lists just to call `.length` on them.
    api.user
      .counts(id)
      .then((c) => !cancelled && setCounts(c))
      .catch(() => !cancelled && setCounts({ followers: 0, following: 0 }));
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (loading) {
    return (
      <div>
        <HeroSkeleton />
        <SectionHeader title="Public playlists" />
        <GridSkeleton count={6} />
      </div>
    );
  }
  if (error || !user) return <ErrorView error={error ?? "User not found"} />;

  // Pull the logged-in user's id off the auth hook so the Follow
  // button can hide itself when viewing your own profile. Server-
  // side already handles the self-follow case, but the button would
  // be misleading.
  const currentUserId = auth.user_id ?? null;

  return (
    <div>
      <DetailHero
        eyebrow="Profile"
        title={user.name || "User"}
        cover={user.picture}
        round
        meta={
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {counts && (
              <>
                <UserLinkStat
                  label="followers"
                  count={counts.followers}
                  to={`/user/${user.id}/followers`}
                />
                <span className="text-muted-foreground">·</span>
                <UserLinkStat
                  label="following"
                  count={counts.following}
                  to={`/user/${user.id}/following`}
                />
              </>
            )}
            {playlists && playlists.length > 0 && (
              <>
                <span className="text-muted-foreground">·</span>
                <span>
                  {playlists.length}{" "}
                  {playlists.length === 1 ? "playlist" : "playlists"}
                </span>
              </>
            )}
          </div>
        }
        actions={
          <FollowButton userId={user.id} currentUserId={currentUserId} />
        }
      />

      <SectionHeader title="Public playlists" />
      {!playlists && <GridSkeleton count={6} />}
      {playlists && playlists.length === 0 && (
        <div className="rounded-lg border border-border/50 bg-card/40 p-6 text-sm text-muted-foreground">
          <UserIcon className="mb-2 h-5 w-5" />
          This user hasn't published any public playlists yet.
        </div>
      )}
      {playlists && playlists.length > 0 && (
        <Grid>
          {playlists.map((p) => (
            <MediaCard key={p.id} item={p} onDownload={onDownload} />
          ))}
        </Grid>
      )}
    </div>
  );
}

function UserLinkStat({
  label,
  count,
  to,
}: {
  label: string;
  count: number;
  to: string;
}) {
  return (
    <Link to={to} className="hover:text-foreground hover:underline">
      <span className="font-semibold text-foreground">{count}</span> {label}
    </Link>
  );
}
