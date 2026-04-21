import { useEffect, useState } from "react";
import { Check, Loader2, UserPlus } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/toast";

/**
 * Toggleable follow button for another user's profile. Follow state
 * lives server-side; we fetch once on mount and optimistically flip
 * on click. Rolls back on API error.
 *
 * Disabled when pointing at yourself — the backend already handles
 * this case gracefully but we hide the affordance so it's not
 * misleading.
 */
export function FollowButton({
  userId,
  currentUserId,
}: {
  userId: string;
  currentUserId: string | null;
}) {
  const toast = useToast();
  const [following, setFollowing] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const isSelf = currentUserId != null && currentUserId === userId;

  useEffect(() => {
    if (isSelf) {
      setFollowing(false);
      return;
    }
    let cancelled = false;
    api.user
      .isFollowing(userId)
      .then((res) => {
        if (!cancelled) setFollowing(res.following);
      })
      .catch(() => {
        if (!cancelled) setFollowing(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, isSelf]);

  if (isSelf) return null;

  const onClick = async () => {
    if (busy || following === null) return;
    setBusy(true);
    const next = !following;
    setFollowing(next); // optimistic
    try {
      const res = next
        ? await api.user.follow(userId)
        : await api.user.unfollow(userId);
      if (!res.ok) {
        setFollowing(!next); // rollback
        toast.show({
          kind: "error",
          title: next ? "Couldn't follow" : "Couldn't unfollow",
          description: res.error ?? undefined,
        });
      }
    } catch (err) {
      setFollowing(!next);
      toast.show({
        kind: "error",
        title: next ? "Couldn't follow" : "Couldn't unfollow",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      variant={following ? "outline" : "default"}
      onClick={onClick}
      disabled={busy || following === null}
    >
      {busy ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : following ? (
        <Check className="h-4 w-4" />
      ) : (
        <UserPlus className="h-4 w-4" />
      )}
      {following ? "Following" : "Follow"}
    </Button>
  );
}
