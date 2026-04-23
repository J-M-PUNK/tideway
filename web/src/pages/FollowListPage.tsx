import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronLeft, User as UserIcon } from "lucide-react";
import { api } from "@/api/client";
import type { TidalUser } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ErrorView } from "@/components/ErrorView";
import { Skeleton } from "@/components/Skeletons";
import { imageProxy } from "@/lib/utils";

/**
 * Lists another user's followers or following. One component with a
 * `kind` prop so we share the layout and skeleton between the two
 * list routes (`/user/:id/followers` and `/user/:id/following`).
 */
export function FollowListPage({ kind }: { kind: "followers" | "following" }) {
  const { id = "" } = useParams();
  const [users, setUsers] = useState<TidalUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setUsers(null);
    setError(null);
    const fetcher =
      kind === "followers" ? api.user.followers : api.user.following;
    fetcher(id)
      .then((rows) => {
        if (!cancelled) setUsers(rows);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [id, kind]);

  if (error) return <ErrorView error={error} />;

  const title = kind === "followers" ? "Followers" : "Following";

  return (
    <div>
      <div className="mb-6 flex items-center gap-3">
        <Button asChild variant="ghost" size="sm">
          <Link to={`/user/${id}`}>
            <ChevronLeft className="h-4 w-4" /> Profile
          </Link>
        </Button>
        <h1 className="text-3xl font-bold tracking-tight">{title}</h1>
      </div>

      {!users && (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      )}
      {users && users.length === 0 && (
        <div className="rounded-lg border border-border/50 bg-card/40 p-6 text-sm text-muted-foreground">
          <UserIcon className="mb-2 h-5 w-5" />
          {kind === "followers"
            ? "No followers yet."
            : "Not following anyone yet."}
        </div>
      )}
      {users && users.length > 0 && (
        <div className="flex flex-col gap-1">
          {users.map((u) => (
            <UserRow key={u.id} user={u} />
          ))}
        </div>
      )}
    </div>
  );
}

function UserRow({ user }: { user: TidalUser }) {
  const avatar = user.picture ? imageProxy(user.picture) : undefined;
  const initial = (user.name || "?").trim().charAt(0).toUpperCase();
  return (
    <Link
      to={`/user/${user.id}`}
      className="group flex items-center gap-3 rounded-md p-3 transition-colors hover:bg-accent"
    >
      <div className="flex h-12 w-12 flex-shrink-0 items-center justify-center overflow-hidden rounded-full bg-secondary text-lg font-bold">
        {avatar ? (
          <img
            src={avatar}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
          />
        ) : (
          <span>{initial}</span>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate font-semibold">{user.name || "User"}</div>
      </div>
    </Link>
  );
}
