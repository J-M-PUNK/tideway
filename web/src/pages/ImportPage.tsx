import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ExternalLink,
  FileText,
  ImportIcon,
  Loader2,
  Music,
  Upload,
} from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/toast";
import { EmptyState } from "@/components/EmptyState";
import { Skeleton } from "@/components/Skeletons";
import { imageProxy, cn } from "@/lib/utils";

/**
 * Playlist import hub. Two sources today:
 *
 *   1. Spotify OAuth — pick from the user's Spotify playlists and
 *      import one at a time. Handles the PKCE dance itself.
 *   2. File / text — paste or drag-drop an M3U / M3U8 / plain text
 *      playlist. Works for any source we don't have OAuth for
 *      (iTunes exports, MusicBee, Apple Music via third-party
 *      exporters, random lists, etc.).
 *
 * Both flows produce the same "match review" payload shape, so the
 * review + create screen is shared. Adding Deezer later is mostly
 * OAuth plumbing — nothing below the match step has to change.
 */

type SpotifyStatus = Awaited<
  ReturnType<typeof api.import.spotify.status>
>;
type SpotifyPlaylist = Awaited<
  ReturnType<typeof api.import.spotify.playlists>
>[number];
type MatchResult = Awaited<ReturnType<typeof api.import.spotify.match>>;
type MatchRow = MatchResult["rows"][number];

type Source = "spotify" | "deezer" | "text";

export function ImportPage() {
  const [source, setSource] = useState<Source>("spotify");
  // One piece of state covers both flows — null = picker screen,
  // populated = review screen. The creation step is identical either
  // way (generic /api/import/create endpoint).
  const [review, setReview] = useState<
    | {
        rows: MatchRow[];
        defaultName: string;
        defaultDescription: string;
      }
    | null
  > (null);

  return (
    <div>
      <Header />
      {review ? (
        <MatchReview
          rows={review.rows}
          defaultName={review.defaultName}
          defaultDescription={review.defaultDescription}
          onBack={() => setReview(null)}
          onDone={() => setReview(null)}
        />
      ) : (
        <>
          <SourceTabs value={source} onChange={setSource} />
          {source === "spotify" && (
            <SpotifyFlow
              onReview={(rows, name, description) =>
                setReview({
                  rows,
                  defaultName: name,
                  defaultDescription: description,
                })
              }
            />
          )}
          {source === "deezer" && (
            <DeezerFlow
              onReview={(rows, name, description) =>
                setReview({
                  rows,
                  defaultName: name,
                  defaultDescription: description,
                })
              }
            />
          )}
          {source === "text" && (
            <TextFlow
              onReview={(rows, name) =>
                setReview({
                  rows,
                  defaultName: name,
                  defaultDescription: "Imported playlist",
                })
              }
            />
          )}
        </>
      )}
    </div>
  );
}

function Header() {
  return (
    <div className="mb-6">
      <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
        <ImportIcon className="h-7 w-7" /> Import playlists
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Pull playlists from Spotify or an M3U / text file into Tidal. Every
        track is matched by ISRC (when known) or by fuzzy title + artist;
        you review the matches before anything is created.
      </p>
    </div>
  );
}

function SourceTabs({
  value,
  onChange,
}: {
  value: Source;
  onChange: (s: Source) => void;
}) {
  return (
    <div className="mb-6 inline-flex rounded-md border border-border bg-secondary p-0.5">
      {(
        [
          { id: "spotify" as const, label: "Spotify", icon: Music },
          { id: "deezer" as const, label: "Deezer", icon: Music },
          { id: "text" as const, label: "File / Text", icon: FileText },
        ]
      ).map(({ id, label, icon: Icon }) => (
        <button
          key={id}
          type="button"
          onClick={() => onChange(id)}
          className={cn(
            "flex items-center gap-2 rounded-sm px-3 py-1.5 text-sm transition-colors",
            value === id
              ? "bg-background font-semibold text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Icon className="h-4 w-4" />
          {label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Spotify flow (OAuth → pick playlist → match → hand off to review)
// ---------------------------------------------------------------------------

function SpotifyFlow({
  onReview,
}: {
  onReview: (rows: MatchRow[], name: string, description: string) => void;
}) {
  const [status, setStatus] = useState<SpotifyStatus | null>(null);
  const [matchingFor, setMatchingFor] = useState<SpotifyPlaylist | null>(null);

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

  if (!status) return <Skeleton className="h-24 w-full" />;

  if (!status.connected) {
    return (
      <ConnectForm
        status={status}
        onConnected={() =>
          api.import.spotify.status().then(setStatus).catch(() => {})
        }
      />
    );
  }

  if (matchingFor) {
    return (
      <PlaylistMatching
        playlist={matchingFor}
        onReady={(rows) =>
          onReview(rows, matchingFor.name, matchingFor.description || "")
        }
        onBack={() => setMatchingFor(null)}
      />
    );
  }

  return (
    <>
      {status.username && (
        <div className="mb-4 text-xs text-muted-foreground">
          Connected as{" "}
          <span className="font-medium text-foreground">{status.username}</span>
          {" · "}
          <button
            onClick={async () => {
              await api.import.spotify.disconnect();
              setStatus({
                ...status,
                connected: false,
                username: null,
              });
            }}
            className="underline hover:text-foreground"
          >
            Disconnect
          </button>
        </div>
      )}
      <PlaylistPicker onPick={setMatchingFor} />
    </>
  );
}

function ConnectForm({
  status,
  onConnected,
}: {
  status: SpotifyStatus;
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

  return (
    <div className="max-w-xl rounded-lg border border-border/50 bg-card/40 p-6">
      <h2 className="text-lg font-semibold">Connect your Spotify account</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        One-time setup. Register a free Spotify Developer app, paste its
        client ID, authorize in your browser, and your playlists show up here.
      </p>

      <ol className="mt-4 list-decimal space-y-2 pl-5 text-sm">
        <li>
          Go to{" "}
          <button
            onClick={() =>
              api
                .openExternal("https://developer.spotify.com/dashboard")
                .catch(() => {})
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
            {status.redirect_uri ||
              "http://127.0.0.1:47823/api/import/spotify/callback"}
          </code>
        </li>
        <li>
          Copy the app's <strong>Client ID</strong> and paste it below.
        </li>
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
            <Button size="sm" variant="outline" onClick={onConnected}>
              I've authorized
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function PlaylistPicker({
  onPick,
}: {
  onPick: (p: SpotifyPlaylist) => void;
}) {
  const toast = useToast();
  const [lists, setLists] = useState<SpotifyPlaylist[] | null>(null);

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

function PlaylistMatching({
  playlist,
  onReady,
  onBack,
}: {
  playlist: SpotifyPlaylist;
  onReady: (rows: MatchRow[]) => void;
  onBack: () => void;
}) {
  const toast = useToast();
  useEffect(() => {
    let cancelled = false;
    api.import.spotify
      .match(playlist.id)
      .then((r) => {
        if (cancelled) return;
        onReady(r.rows);
      })
      .catch((err) => {
        if (cancelled) return;
        toast.show({
          kind: "error",
          title: "Matching failed",
          description: err instanceof Error ? err.message : String(err),
        });
        onBack();
      });
    return () => {
      cancelled = true;
    };
  }, [playlist.id, onReady, onBack, toast]);
  return (
    <div className="flex flex-col gap-3">
      <Button size="sm" variant="ghost" onClick={onBack}>
        <ChevronLeft className="h-4 w-4" /> Cancel
      </Button>
      <div className="rounded-lg border border-border/50 bg-card/40 p-5 text-sm text-muted-foreground">
        <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
        Matching {playlist.tracks} tracks against Tidal…
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Deezer flow (paste URL → fetch public playlist → match)
// ---------------------------------------------------------------------------

function DeezerFlow({
  onReview,
}: {
  onReview: (rows: MatchRow[], name: string, description: string) => void;
}) {
  const toast = useToast();
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async () => {
    const s = source.trim();
    if (!s) {
      toast.show({
        kind: "info",
        title: "Paste a Deezer playlist URL or id",
      });
      return;
    }
    setBusy(true);
    try {
      const res = await api.import.deezer.match(s);
      if (res.total === 0) {
        toast.show({
          kind: "info",
          title: "Empty playlist",
          description:
            "Deezer returned no tracks — make sure the playlist is public.",
        });
        return;
      }
      onReview(
        res.rows,
        res.playlist.name || "Imported from Deezer",
        res.playlist.description || "Imported playlist",
      );
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't fetch playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-xl rounded-lg border border-border/50 bg-card/40 p-6">
      <h2 className="text-lg font-semibold">From a Deezer playlist</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Paste the link to any <strong>public</strong> Deezer playlist. No
        sign-in needed on our end — we just fetch the track list from
        Deezer's public API and match it against Tidal. Private playlists:
        open the playlist in Deezer, set it to public, import, set it back.
      </p>

      <div className="mt-5 flex flex-col gap-2">
        <label className="text-xs font-semibold text-muted-foreground">
          Playlist URL or id
        </label>
        <Input
          value={source}
          onChange={(e) => setSource(e.target.value)}
          placeholder="https://www.deezer.com/playlist/1234567890"
          spellCheck={false}
        />
      </div>

      <div className="mt-4">
        <Button onClick={run} disabled={busy || !source.trim()}>
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Check className="h-4 w-4" />
          )}
          Fetch + match
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Text / file flow
// ---------------------------------------------------------------------------

function TextFlow({
  onReview,
}: {
  onReview: (rows: MatchRow[], name: string) => void;
}) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [name, setName] = useState("Imported playlist");
  const [busy, setBusy] = useState(false);

  const onFile = async (file: File | null) => {
    if (!file) return;
    try {
      const content = await file.text();
      setText(content);
      // Default the playlist name to the filename (stripped extension).
      const base = file.name.replace(/\.(m3u8?|txt|csv|tsv)$/i, "");
      setName(base || file.name);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't read file",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const run = async () => {
    if (!text.trim()) {
      toast.show({ kind: "info", title: "Paste or upload a playlist first" });
      return;
    }
    setBusy(true);
    try {
      const res = await api.import.text.parse(text);
      if (res.total === 0) {
        toast.show({
          kind: "info",
          title: "No tracks found",
          description: "The input didn't look like a playlist we could parse.",
        });
        return;
      }
      onReview(res.rows, name || "Imported playlist");
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Match failed",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-2xl rounded-lg border border-border/50 bg-card/40 p-6">
      <h2 className="text-lg font-semibold">From a playlist file</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Works with .m3u / .m3u8 files from iTunes, MusicBee, Plex, or any
        exporter that produces the standard format. Plain text also works —
        one{" "}
        <code className="rounded bg-secondary px-1">Artist - Title</code> per
        line.
      </p>

      <div className="mt-5 flex flex-col gap-2">
        <label className="text-xs font-semibold text-muted-foreground">
          Playlist name
        </label>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="My imported playlist"
        />
      </div>

      <div className="mt-5 flex flex-col gap-2">
        <label className="text-xs font-semibold text-muted-foreground">
          Paste content
        </label>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={10}
          spellCheck={false}
          className="rounded-md border border-input bg-secondary px-3 py-2 text-xs font-mono"
          placeholder={"#EXTM3U\n#EXTINF:183,The Beatles - Something\n...\n\nOr just:\nThe Beatles - Something\nRadiohead - Paranoid Android"}
        />
      </div>

      <div className="mt-4 flex items-center gap-3">
        <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-secondary px-3 py-1.5 text-xs font-semibold hover:bg-accent">
          <Upload className="h-3.5 w-3.5" />
          Upload file
          <input
            type="file"
            accept=".m3u,.m3u8,.txt,.csv,.tsv,text/plain"
            onChange={(e) => onFile(e.target.files?.[0] ?? null)}
            className="hidden"
          />
        </label>
        <div className="flex-1" />
        <Button onClick={run} disabled={busy || !text.trim()}>
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Check className="h-4 w-4" />
          )}
          Match against Tidal
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Match review + create (shared)
// ---------------------------------------------------------------------------

function MatchReview({
  rows,
  defaultName,
  defaultDescription,
  onBack,
  onDone,
}: {
  rows: MatchRow[];
  defaultName: string;
  defaultDescription: string;
  onBack: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(defaultName);
  const [selected, setSelected] = useState<Set<string>>(() => {
    // Auto-select high-confidence matches (ISRC or fuzzy ≥ 0.7).
    const auto = new Set<string>();
    for (const row of rows) {
      if (row.match && row.match.confidence >= 0.7) {
        auto.add(row.match.tidal_id);
      }
    }
    return auto;
  });
  const [creating, setCreating] = useState(false);

  const toggle = (id: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const matched = rows.filter((r) => r.match !== null).length;
  const unmatched = rows.length - matched;

  const create = async () => {
    if (selected.size === 0) {
      toast.show({ kind: "info", title: "Pick at least one track" });
      return;
    }
    setCreating(true);
    try {
      const res = await api.import.create(
        name,
        defaultDescription,
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

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <Button size="sm" variant="ghost" onClick={onBack} disabled={creating}>
          <ChevronLeft className="h-4 w-4" /> Back
        </Button>
        <Button onClick={create} disabled={creating || selected.size === 0}>
          {creating ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Check className="h-4 w-4" />
          )}
          Create "{name}" with {selected.size} tracks
        </Button>
      </div>

      <div className="mb-4 flex flex-col gap-3 rounded-lg border border-border/50 bg-card/40 p-4">
        <div className="text-sm font-semibold">
          Matched {matched} of {rows.length} tracks
          {unmatched > 0 && (
            <span className="ml-2 text-amber-300">
              · {unmatched} couldn't be found on Tidal
            </span>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs font-semibold text-muted-foreground">
            Playlist name
          </label>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Playlist name"
          />
        </div>
        <div className="text-xs text-muted-foreground">
          High-confidence matches (ISRC or strong fuzzy match) are
          pre-selected. Uncheck any you'd rather skip.
        </div>
      </div>

      <div className="flex flex-col divide-y divide-border/40 rounded-lg border border-border/50 bg-card/40">
        {rows.map((row, i) => (
          <MatchRowView
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

function MatchRowView({
  row,
  checked,
  onToggle,
}: {
  row: MatchRow;
  checked: boolean;
  onToggle?: () => void;
}) {
  const source = row.spotify;
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
        <div className="truncate font-medium">{source.name}</div>
        <div className="truncate text-xs text-muted-foreground">
          {source.artists.join(", ") || "(no artist)"}
        </div>
      </div>
      {match ? (
        <div className="flex min-w-0 items-center gap-2">
          <div
            className={cn(
              "text-xs font-semibold uppercase tracking-wider",
              match.confidence >= 0.85
                ? "text-primary"
                : match.confidence >= 0.7
                  ? "text-muted-foreground"
                  : "text-amber-400",
            )}
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
