import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ExternalLink,
  ImportIcon,
  Loader2,
  Music,
} from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/toast";
import { EmptyState } from "@/components/EmptyState";
import { Skeleton } from "@/components/Skeletons";
import { imageProxy } from "@/lib/utils";

/**
 * Spotify → Tidal playlist import.
 *
 * Three states:
 *   1. Not connected → setup form (paste client_id, open auth URL in
 *      an external browser, come back and click "I've authorized").
 *   2. Connected, no playlist selected → playlist picker.
 *   3. Playlist selected → match preview with per-row checkboxes +
 *      "Create Tidal playlist" button.
 *
 * The user has to register a Spotify Developer app (free) and paste
 * its client_id — same friction model as Last.fm. Docs linked inline.
 */

type Status = Awaited<ReturnType<typeof api.import.spotify.status>>;
type Playlist = Awaited<
  ReturnType<typeof api.import.spotify.playlists>
>[number];
type MatchResult = Awaited<ReturnType<typeof api.import.spotify.match>>;

export function ImportPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [selected, setSelected] = useState<Playlist | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.import.spotify
      .status()
      .then((s) => !cancelled && setStatus(s))
      .catch(() => {
        if (!cancelled)
          setStatus({
            connected: false,
            username: null,
            client_id_set: false,
            redirect_uri: "",
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!status) {
    return (
      <div>
        <Header />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (!status.connected) {
    return (
      <div>
        <Header />
        <ConnectForm
          status={status}
          onConnected={() =>
            api.import.spotify.status().then(setStatus).catch(() => {})
          }
        />
      </div>
    );
  }

  if (selected) {
    return (
      <div>
        <Header />
        <MatchReview
          playlist={selected}
          onBack={() => setSelected(null)}
          onDone={() => setSelected(null)}
        />
      </div>
    );
  }

  return (
    <div>
      <Header username={status.username} />
      <PlaylistPicker onPick={setSelected} />
    </div>
  );
}

function Header({ username }: { username?: string | null }) {
  return (
    <div className="mb-6">
      <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
        <ImportIcon className="h-7 w-7" /> Import from Spotify
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Pull your Spotify playlists into Tidal. Each track is matched to
        Tidal by ISRC or by fuzzy title + artist; you review the matches
        before anything is created.
        {username && (
          <>
            {" "}
            Connected as{" "}
            <span className="font-medium text-foreground">{username}</span>.
          </>
        )}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Setup: paste client_id, open Spotify auth
// ---------------------------------------------------------------------------

function ConnectForm({
  status,
  onConnected,
}: {
  status: Status;
  onConnected: () => void;
}) {
  const toast = useToast();
  const [clientId, setClientId] = useState("");
  const [busy, setBusy] = useState(false);
  const [awaiting, setAwaiting] = useState(false);

  const start = async () => {
    const id = clientId.trim();
    if (!id) {
      toast.show({ kind: "error", title: "client_id required" });
      return;
    }
    setBusy(true);
    try {
      const { auth_url } = await api.import.spotify.connect(id);
      // Open in the system browser so the user sees Spotify's real
      // login — we never see their password.
      await api.openExternal(auth_url);
      setAwaiting(true);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start Spotify auth",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  const recheck = async () => {
    setBusy(true);
    try {
      onConnected();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-xl rounded-lg border border-border/50 bg-card/40 p-6">
      <h2 className="text-lg font-semibold">Connect your Spotify account</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        One-time setup. Register a free Spotify Developer app, paste its
        client ID below, authorize in your browser, and your playlists
        show up here.
      </p>

      <ol className="mt-4 list-decimal space-y-2 pl-5 text-sm">
        <li>
          Go to{" "}
          <button
            onClick={() =>
              api.openExternal("https://developer.spotify.com/dashboard").catch(() => {})
            }
            className="text-primary hover:underline"
          >
            developer.spotify.com/dashboard
          </button>{" "}
          and create a new app.
        </li>
        <li>
          Add this as a Redirect URI exactly:
          <code className="mt-1 block w-fit rounded bg-secondary px-2 py-1 text-xs">
            {status.redirect_uri || "http://127.0.0.1:47823/api/import/spotify/callback"}
          </code>
        </li>
        <li>Copy the app's <strong>Client ID</strong> and paste it below.</li>
      </ol>

      <div className="mt-5 flex flex-col gap-2">
        <label className="text-xs font-semibold text-muted-foreground">
          Spotify client ID
        </label>
        <Input
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder="32-character hex string"
          spellCheck={false}
        />
      </div>

      <div className="mt-5 flex items-center gap-3">
        {!awaiting ? (
          <Button onClick={start} disabled={busy}>
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <ExternalLink className="h-4 w-4" />
            )}
            Authorize in browser
          </Button>
        ) : (
          <>
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Waiting for you to approve in the browser…
            </div>
            <Button size="sm" variant="outline" onClick={recheck}>
              I've authorized
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Playlist picker
// ---------------------------------------------------------------------------

function PlaylistPicker({ onPick }: { onPick: (p: Playlist) => void }) {
  const toast = useToast();
  const [lists, setLists] = useState<Playlist[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.import.spotify
      .playlists()
      .then((ps) => !cancelled && setLists(ps))
      .catch((err) => {
        if (cancelled) return;
        setLists([]);
        toast.show({
          kind: "error",
          title: "Couldn't load playlists",
          description: err instanceof Error ? err.message : String(err),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [toast]);

  if (!lists) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-16 w-full" />
        ))}
      </div>
    );
  }

  if (lists.length === 0) {
    return (
      <EmptyState
        icon={Music}
        title="No playlists found"
        description="Your Spotify account doesn't have any playlists we can read."
      />
    );
  }

  return (
    <div className="flex flex-col gap-1">
      {lists.map((p) => (
        <button
          key={p.id}
          type="button"
          onClick={() => onPick(p)}
          className="group flex items-center gap-4 rounded-md px-3 py-2 text-left transition-colors hover:bg-accent"
        >
          <div className="h-14 w-14 flex-shrink-0 overflow-hidden rounded-md bg-secondary">
            {p.image ? (
              <img
                src={imageProxy(p.image) ?? p.image}
                alt=""
                className="h-full w-full object-cover"
                loading="lazy"
              />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-muted-foreground">
                <Music className="h-6 w-6" />
              </div>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate font-medium">{p.name}</div>
            <div className="truncate text-xs text-muted-foreground">
              {p.tracks} {p.tracks === 1 ? "track" : "tracks"}
              {p.owner ? ` · ${p.owner}` : ""}
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Match review + create
// ---------------------------------------------------------------------------

function MatchReview({
  playlist,
  onBack,
  onDone,
}: {
  playlist: Playlist;
  onBack: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const [result, setResult] = useState<MatchResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.import.spotify
      .match(playlist.id)
      .then((r) => {
        if (cancelled) return;
        setResult(r);
        // Default: select every row with a match at confidence >=
        // 0.7, the threshold where we're pretty sure it's the right
        // track. Users can hand-pick the rest.
        const auto = new Set<string>();
        for (const row of r.rows) {
          if (row.match && row.match.confidence >= 0.7) {
            auto.add(row.match.tidal_id);
          }
        }
        setSelected(auto);
      })
      .catch((err) => {
        if (cancelled) return;
        toast.show({
          kind: "error",
          title: "Matching failed",
          description: err instanceof Error ? err.message : String(err),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [playlist.id, toast]);

  if (!result) {
    return (
      <div className="flex flex-col gap-3">
        <Button size="sm" variant="ghost" onClick={onBack}>
          <ChevronLeft className="h-4 w-4" /> Back to playlists
        </Button>
        <div className="rounded-lg border border-border/50 bg-card/40 p-5 text-sm text-muted-foreground">
          <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
          Matching {playlist.tracks} tracks against Tidal…
        </div>
      </div>
    );
  }

  const toggle = (id: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const create = async () => {
    if (selected.size === 0) {
      toast.show({
        kind: "info",
        title: "Pick at least one track",
      });
      return;
    }
    setCreating(true);
    try {
      const res = await api.import.spotify.create(
        playlist.name,
        playlist.description || "Imported from Spotify",
        Array.from(selected),
      );
      toast.show({
        kind: "success",
        title: `Created "${res.name}"`,
        description: `${res.added} tracks added${res.failed ? `, ${res.failed} failed` : ""}.`,
      });
      onDone();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't create playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setCreating(false);
    }
  };

  const unmatched = result.rows.filter((r) => r.match === null).length;

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <Button size="sm" variant="ghost" onClick={onBack} disabled={creating}>
          <ChevronLeft className="h-4 w-4" /> Back
        </Button>
        <Button onClick={create} disabled={creating}>
          {creating ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Check className="h-4 w-4" />
          )}
          Create "{playlist.name}" with {selected.size} tracks
        </Button>
      </div>

      <div className="mb-4 rounded-lg border border-border/50 bg-card/40 p-4 text-sm">
        <div className="font-semibold">
          Matched {result.matched} of {result.total} tracks
          {unmatched > 0 && (
            <span className="ml-2 text-amber-300">
              · {unmatched} couldn't be found on Tidal
            </span>
          )}
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          Rows with high-confidence matches (ISRC or strong fuzzy match) are
          pre-selected. Uncheck any you'd rather skip.
        </div>
      </div>

      <div className="flex flex-col divide-y divide-border/40 rounded-lg border border-border/50 bg-card/40">
        {result.rows.map((row, i) => (
          <MatchRow
            key={i}
            row={row}
            checked={row.match ? selected.has(row.match.tidal_id) : false}
            onToggle={row.match ? () => toggle(row.match!.tidal_id) : undefined}
          />
        ))}
      </div>
    </div>
  );
}

function MatchRow({
  row,
  checked,
  onToggle,
}: {
  row: MatchResult["rows"][number];
  checked: boolean;
  onToggle?: () => void;
}) {
  const spotify = row.spotify;
  const match = row.match;
  return (
    <div className="flex items-center gap-3 px-4 py-2.5 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        disabled={!onToggle}
        className="h-4 w-4 accent-primary disabled:opacity-30"
      />
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium">{spotify.name}</div>
        <div className="truncate text-xs text-muted-foreground">
          {spotify.artists.join(", ")}
        </div>
      </div>
      {match ? (
        <div className="flex min-w-0 items-center gap-2">
          <div
            className={
              match.confidence >= 0.85
                ? "text-xs font-semibold uppercase tracking-wider text-primary"
                : match.confidence >= 0.7
                  ? "text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                  : "text-xs font-semibold uppercase tracking-wider text-amber-400"
            }
          >
            {match.reason === "isrc"
              ? "ISRC"
              : `${Math.round(match.confidence * 100)}%`}
          </div>
          <div className="min-w-0 text-right">
            <div className="truncate text-xs font-medium">{match.name}</div>
            <div className="truncate text-[11px] text-muted-foreground">
              {match.artists.join(", ")}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-1.5 text-xs text-amber-400">
          <AlertTriangle className="h-3.5 w-3.5" />
          No match
        </div>
      )}
    </div>
  );
}
