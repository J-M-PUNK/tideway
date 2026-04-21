import { useEffect, useRef, useState } from "react";
import {
  Bell,
  Check,
  Download,
  Headphones,
  Keyboard,
  Library as LibraryIcon,
  Loader2,
  LogOut,
  Moon,
  Palette,
  Settings as SettingsIcon,
  Sun,
} from "lucide-react";
import { api } from "@/api/client";
import type { QualityOption, Settings } from "@/api/types";
import { Button } from "@/components/ui/button";
import { publishDefaultQuality } from "@/components/DownloadButton";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/toast";
import { useOfflineMode } from "@/hooks/useOfflineMode";
import {
  useUiPreferences,
  type StreamingQuality,
  type ThemeMode,
} from "@/hooks/useUiPreferences";
import { Skeleton } from "@/components/Skeletons";
import { cn } from "@/lib/utils";

type SaveStatus = "idle" | "saving" | "saved" | "error";

export function SettingsPage({ onLogout }: { onLogout: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [qualities, setQualities] = useState<QualityOption[]>([]);
  const [status, setStatus] = useState<SaveStatus>("idle");
  const toast = useToast();
  const ui = useUiPreferences();
  // Pull out the setter rather than the whole context object: the
  // autosave effect below depends on it, and the setter is a stable
  // useState reference while the context object identity changes
  // every time `offline` flips, which would otherwise spuriously
  // re-run the effect and re-save.
  const { set: setOfflineCtx } = useOfflineMode();

  const [loadError, setLoadError] = useState<Error | null>(null);
  // null until the initial server load completes — that way the
  // autosave effect below can tell "user edited something" apart from
  // "server just handed us the initial snapshot".
  const lastSavedRef = useRef<Settings | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const savedIndicatorRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, qs] = await Promise.all([api.settings.get(), api.qualities()]);
        if (cancelled) return;
        setSettings(s);
        lastSavedRef.current = s;
        setQualities(qs);
      } catch (err) {
        if (!cancelled)
          setLoadError(err instanceof Error ? err : new Error(String(err)));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Autosave. Debounced so text inputs (output_dir, filename_template)
  // don't fire a request per keystroke. Toggles/selects feel instant
  // anyway because the UI reflects the change immediately — the 600ms
  // delay only shows up as a brief "Saving…" blip.
  useEffect(() => {
    if (!settings || !lastSavedRef.current) return;
    if (settings === lastSavedRef.current) return;
    if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(async () => {
      saveTimerRef.current = null;
      setStatus("saving");
      try {
        const saved = await api.settings.put(settings);
        lastSavedRef.current = saved;
        // Server may normalize (e.g. resolve ~ in output_dir). Reflect
        // the normalized value — unless the user has already typed more
        // since we sent the request, in which case their in-flight text
        // wins and we'll converge on the next debounce cycle.
        setSettings((cur) =>
          cur && JSON.stringify(cur) === JSON.stringify(settings) ? saved : cur,
        );
        publishDefaultQuality(saved.quality);
        // Sync the offline-mode context so the app's shell reacts
        // immediately when the user toggles it here — otherwise the
        // sidebar and routes stay out of step until the next reload.
        setOfflineCtx(saved.offline_mode);
        // Broadcast the notification preference so the shell's
        // useDownloadNotifications hook picks up the new value without
        // waiting for a reload. Details payload is untyped on purpose
        // so future settings can piggyback on the same event.
        window.dispatchEvent(
          new CustomEvent("tidal-settings-updated", { detail: saved }),
        );
        setStatus("saved");
        if (savedIndicatorRef.current !== null)
          window.clearTimeout(savedIndicatorRef.current);
        savedIndicatorRef.current = window.setTimeout(() => {
          setStatus((st) => (st === "saved" ? "idle" : st));
          savedIndicatorRef.current = null;
        }, 1500);
      } catch (err) {
        setStatus("error");
        toast.show({
          kind: "error",
          title: "Save failed",
          description: err instanceof Error ? err.message : String(err),
        });
      }
    }, 600);
    // Deliberately NOT returning a cleanup that cancels the timer —
    // doing so would cancel the only in-flight save whenever the
    // component re-renders for unrelated reasons. The next edit
    // clears + reschedules via the ref at the top of the effect.
  }, [settings, toast]);

  useEffect(() => {
    return () => {
      if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
      if (savedIndicatorRef.current !== null) window.clearTimeout(savedIndicatorRef.current);
    };
  }, []);

  if (loadError)
    return (
      <div className="text-sm text-destructive">Couldn't load settings: {loadError.message}</div>
    );
  if (!settings) {
    return (
      <div className="max-w-2xl">
        <Skeleton className="mb-8 h-9 w-48" />
        {Array.from({ length: 3 }, (_, i) => (
          <section
            key={i}
            className="mb-10 flex flex-col gap-5 rounded-lg border border-border/50 bg-card/40 p-6"
          >
            <Skeleton className="h-5 w-32" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-3/4" />
          </section>
        ))}
      </div>
    );
  }

  const patch = (p: Partial<Settings>) => setSettings({ ...settings, ...p });

  return (
    <div className="max-w-2xl">
      <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
        <SettingsIcon className="h-7 w-7" /> Settings
      </h1>

      <Section
        title="Playback"
        icon={Headphones}
        description="Streaming quality, output device, and the equalizer all run through the native audio engine."
      >
        <Field
          label="Streaming quality"
          hint="Used when playing a track that isn't downloaded. Only tiers your subscription supports appear."
        >
          <select
            value={
              // Clamp the stored value against the filtered list so a
              // stale "hi_res_lossless" pref doesn't show an off-list
              // value after a subscription downgrade.
              qualities.some((q) => q.value === ui.streamingQuality)
                ? ui.streamingQuality
                : qualities[0]?.value ?? "low_320k"
            }
            onChange={(e) =>
              ui.set({ streamingQuality: e.target.value as StreamingQuality })
            }
            className="h-10 rounded-md border border-input bg-secondary px-3 text-sm"
          >
            {qualities.map((q) => (
              <option key={q.value} value={q.value}>
                {q.label} — {q.bitrate}
              </option>
            ))}
          </select>
        </Field>
        <AudioEngineFields />
      </Section>

      <Section
        title="Downloads"
        icon={Download}
        description="Where and how your music is saved to disk."
      >
        <Field label="Output folder">
          <Input
            value={settings.output_dir}
            onChange={(e) => patch({ output_dir: e.target.value })}
            placeholder="/path/to/music"
          />
        </Field>

        <Field
          label="Default quality"
          hint="Used for any download that doesn't override it. Your subscription must support the selected quality."
        >
          <select
            value={settings.quality}
            onChange={(e) => patch({ quality: e.target.value })}
            className="h-10 rounded-md border border-input bg-secondary px-3 text-sm"
          >
            {qualities.map((q) => (
              <option key={q.value} value={q.value}>
                {q.label} — {q.codec} · {q.bitrate}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Filename template"
          hint={
            <>
              Tokens: <code>{"{artist}"}</code> <code>{"{title}"}</code>{" "}
              <code>{"{album}"}</code> <code>{"{track_num}"}</code>
            </>
          }
        >
          <Input
            value={settings.filename_template}
            onChange={(e) => patch({ filename_template: e.target.value })}
            placeholder="{artist} - {title}"
          />
        </Field>

        <Toggle
          checked={settings.create_album_folders}
          onChange={(v) => patch({ create_album_folders: v })}
          label="Create a subfolder per album"
        />
        <Toggle
          checked={settings.skip_existing}
          onChange={(v) => patch({ skip_existing: v })}
          label="Skip downloads that already exist on disk"
        />
        <Field
          label={`Concurrent downloads — ${settings.concurrent_downloads}`}
          hint="How many tracks download in parallel. Higher = faster, but risks Tidal rate-limiting."
        >
          <input
            type="range"
            min={1}
            max={10}
            step={1}
            value={settings.concurrent_downloads}
            onChange={(e) => patch({ concurrent_downloads: Number(e.target.value) })}
            className="h-2 w-full cursor-pointer appearance-none rounded-full bg-secondary accent-primary"
            aria-label="Concurrent downloads"
          />
        </Field>
      </Section>

      <Section
        title="Library"
        icon={LibraryIcon}
        description="What shows up in your library and whether the app talks to Tidal."
      >
        <Toggle
          checked={ui.offlineOnly}
          onChange={(v) => ui.set({ offlineOnly: v })}
          label="Show only downloaded tracks in lists"
        />
        <Toggle
          checked={settings.offline_mode}
          onChange={(v) => patch({ offline_mode: v })}
          label="Work offline (hide search, explore, anything needing Tidal)"
        />
      </Section>

      <Section title="Appearance" icon={Palette}>
        <Field label="Theme">
          <ThemePicker value={ui.theme} onChange={(t) => ui.set({ theme: t })} />
        </Field>
      </Section>

      <Section
        title="Notifications"
        icon={Bell}
        description="Desktop notification when a batch of downloads finishes. The browser will prompt for permission the first time it fires."
      >
        <Toggle
          checked={settings.notify_on_complete}
          onChange={(v) => patch({ notify_on_complete: v })}
          label="Notify me when downloads finish"
        />
      </Section>

      <LastFmSection />

      <Section
        title="Keyboard shortcuts"
        icon={Keyboard}
        description="Window must be focused for these to fire. Media keys (Play/Pause/Next/Prev) also work globally — the app listens for them even when minimized."
      >
        <ShortcutRow keys={["⌘", "K"]} label="Focus search" />
        <ShortcutRow keys={["Space"]} label="Play / pause" />
        <ShortcutRow keys={["Shift", "→"]} label="Next track" />
        <ShortcutRow keys={["Shift", "←"]} label="Previous track" />
        <ShortcutRow keys={["M"]} label="Mute / unmute" />
      </Section>

      <div className="mt-8 flex items-center gap-3">
        <Button variant="outline" onClick={onLogout}>
          <LogOut className="h-4 w-4" /> Log out
        </Button>
        <SaveStatus status={status} />
      </div>
    </div>
  );
}

function SaveStatus({ status }: { status: SaveStatus }) {
  if (status === "idle") return null;
  if (status === "saving")
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Saving…
      </span>
    );
  if (status === "saved")
    return (
      <span className="flex items-center gap-1.5 text-xs text-primary">
        <Check className="h-3.5 w-3.5" /> Saved
      </span>
    );
  return <span className="text-xs text-destructive">Save failed</span>;
}

function Section({
  title,
  description,
  icon: Icon,
  children,
}: {
  title: string;
  description?: string;
  icon?: typeof Bell;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-6 flex flex-col gap-5 rounded-lg border border-border/50 bg-card/40 p-6">
      <div>
        <h2 className="flex items-center gap-2 text-lg font-semibold">
          {Icon && <Icon className="h-4 w-4 text-muted-foreground" />}
          {title}
        </h2>
        {description && <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>}
      </div>
      {children}
    </section>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label>{label}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="flex items-center gap-3 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 accent-primary"
      />
      {label}
    </label>
  );
}

function ThemePicker({
  value,
  onChange,
}: {
  value: ThemeMode;
  onChange: (v: ThemeMode) => void;
}) {
  const options: { value: ThemeMode; label: string; icon: typeof Moon }[] = [
    { value: "dark", label: "Dark", icon: Moon },
    { value: "light", label: "Light", icon: Sun },
  ];
  return (
    <div className="inline-flex w-fit rounded-md border border-border bg-secondary p-1">
      {options.map((opt) => {
        const Icon = opt.icon;
        const active = value === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={cn(
              "flex items-center gap-2 rounded px-3 py-1.5 text-sm font-medium transition-colors",
              active
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-4 w-4" />
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Equalizer + audio-output device picker. Both drive libvlc directly
 * and both are persisted in the backend settings.json so a relaunch
 * keeps the user's sound + device.
 *
 * Rendered as Fields (no outer Section) so they compose into the
 * parent Playback section alongside the streaming-quality picker.
 *
 * Preset dropdown picks one of libvlc's 18 built-ins and lets the
 * backend resolve to per-band amplitudes (so "Rock" renders the
 * matching slider curve immediately). Manual slider changes POST the
 * full band array with `preamp` = null ("leave preamp at libvlc's
 * default" vs. an explicit number).
 */
function AudioEngineFields() {
  const toast = useToast();
  const [eq, setEq] = useState<{
    bands: number[];
    preamp: number | null;
    band_count: number;
    frequencies: number[];
    presets: { index: number; name: string }[];
  } | null>(null);
  const [devices, setDevices] = useState<{
    devices: { id: string; name: string }[];
    current: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [e, d] = await Promise.all([
          api.player.eq(),
          api.player.outputDevices(),
        ]);
        if (cancelled) return;
        setEq(e);
        setDevices(d);
      } catch {
        /* libvlc not available — fields stay hidden */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Local mirror of the slider state so dragging is instant; flush to
  // the backend on release (via mouse-up / change commit).
  const [localBands, setLocalBands] = useState<number[] | null>(null);
  useEffect(() => {
    if (eq) setLocalBands(eq.bands.length ? eq.bands : new Array(eq.band_count).fill(0));
  }, [eq]);

  if (!eq || !devices) return null;

  const flush = async (bands: number[]) => {
    setLocalBands(bands);
    try {
      await api.player.setEq(bands, null);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't apply EQ",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const pickPreset = async (idx: number) => {
    try {
      const res = await api.player.setEqPreset(idx);
      setLocalBands(res.bands);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't apply preset",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const reset = () => {
    const flat = new Array(eq.band_count).fill(0);
    flush(flat);
  };

  const pickDevice = async (id: string) => {
    try {
      await api.player.setOutputDevice(id);
      setDevices({ ...devices, current: id });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't switch output device",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <>
      <Field label="Output device">
        <select
          value={devices.current}
          onChange={(e) => pickDevice(e.target.value)}
          className="h-10 rounded-md border border-input bg-secondary px-3 text-sm"
        >
          {devices.devices.map((d) => (
            <option key={d.id || "default"} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
      </Field>

      <Field
        label="Equalizer"
        hint="Drag sliders or pick a preset. Reset flattens to zero."
      >
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <select
              onChange={(e) => {
                const n = parseInt(e.target.value, 10);
                if (!isNaN(n)) pickPreset(n);
              }}
              value=""
              className="h-9 rounded-md border border-input bg-secondary px-3 text-xs"
            >
              <option value="">Presets…</option>
              {eq.presets.map((p) => (
                <option key={p.index} value={p.index}>
                  {p.name}
                </option>
              ))}
            </select>
            <Button size="sm" variant="outline" onClick={reset}>
              Reset
            </Button>
          </div>

          <div className="flex items-end gap-3">
            {localBands?.map((v, i) => (
              <EqSlider
                key={i}
                value={v}
                freq={eq.frequencies[i]}
                onChange={(nv) => {
                  const next = [...localBands];
                  next[i] = nv;
                  setLocalBands(next);
                }}
                onCommit={(nv) => {
                  const next = [...localBands];
                  next[i] = nv;
                  flush(next);
                }}
              />
            ))}
          </div>
        </div>
      </Field>
    </>
  );
}

function EqSlider({
  value,
  freq,
  onChange,
  onCommit,
}: {
  value: number;
  freq: number;
  onChange: (v: number) => void;
  onCommit: (v: number) => void;
}) {
  const label =
    freq >= 1000 ? `${Math.round(freq / 100) / 10}k` : `${Math.round(freq)}`;
  return (
    <div className="flex flex-col items-center gap-2">
      <div className="text-xs tabular-nums text-muted-foreground">
        {value >= 0 ? "+" : ""}
        {value.toFixed(1)}
      </div>
      <input
        type="range"
        min={-20}
        max={20}
        step={0.5}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        onMouseUp={(e) =>
          onCommit(parseFloat((e.target as HTMLInputElement).value))
        }
        onTouchEnd={(e) =>
          onCommit(parseFloat((e.target as HTMLInputElement).value))
        }
        onKeyUp={(e) =>
          onCommit(parseFloat((e.target as HTMLInputElement).value))
        }
        className="eq-slider h-32 w-4 accent-primary"
        style={
          {
            writingMode: "vertical-lr",
            direction: "rtl",
          } as React.CSSProperties
        }
      />
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}


/**
 * Last.fm scrobbling setup. Three states:
 *  1. No API credentials → show a "why" blurb + key/secret inputs +
 *     a deep link to last.fm's API-account page.
 *  2. Credentials set but not yet connected → a button that opens the
 *     browser to last.fm's auth URL and a "I've approved" confirmation.
 *  3. Connected → username + disconnect button.
 *
 * Lives as its own component so the three-state machine stays compact
 * and doesn't complicate the main settings body.
 */
function LastFmSection() {
  const toast = useToast();
  const [status, setStatus] = useState<{
    has_credentials: boolean;
    using_default_credentials: boolean;
    connected: boolean;
    username: string | null;
  } | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "save" | "connect" | "complete" | "disconnect">(null);

  useEffect(() => {
    let cancelled = false;
    api.lastfm
      .status()
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch(() => {
        if (!cancelled)
          setStatus({
            has_credentials: false,
            using_default_credentials: false,
            connected: false,
            username: null,
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const saveCreds = async () => {
    if (!apiKey.trim() || !apiSecret.trim()) return;
    setBusy("save");
    try {
      const s = await api.lastfm.setCredentials(apiKey.trim(), apiSecret.trim());
      setStatus(s);
      setApiKey("");
      setApiSecret("");
      toast.show({ kind: "success", title: "API credentials saved" });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't save credentials",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  };

  const startConnect = async () => {
    setBusy("connect");
    try {
      const { auth_url, token } = await api.lastfm.connectStart();
      setPendingToken(token);
      // pywebview drops window.open for cross-origin URLs; route
      // through the backend so Python's webbrowser module launches
      // the system default. Fallback to window.open for dev browser mode.
      try {
        await api.openExternal(auth_url);
      } catch {
        window.open(auth_url, "_blank", "noopener");
      }
      toast.show({
        kind: "info",
        title: "Approve in browser",
        description: "Then click Continue to finish connecting.",
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start Last.fm auth",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  };

  const completeConnect = async () => {
    if (!pendingToken) return;
    setBusy("complete");
    try {
      const { username } = await api.lastfm.connectComplete(pendingToken);
      setPendingToken(null);
      const s = await api.lastfm.status();
      setStatus(s);
      toast.show({
        kind: "success",
        title: "Last.fm connected",
        description: `Scrobbling as ${username}`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't finish connecting",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async () => {
    setBusy("disconnect");
    try {
      const s = await api.lastfm.disconnect();
      setStatus(s);
      setPendingToken(null);
      toast.show({ kind: "info", title: "Disconnected from Last.fm" });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't disconnect",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  };

  const openApiAccountPage = async () => {
    const url = "https://www.last.fm/api/account/create";
    try {
      await api.openExternal(url);
    } catch {
      window.open(url, "_blank", "noopener");
    }
  };

  return (
    <Section
      title="Last.fm"
      description="Track your listening history with Last.fm. Tidal doesn't share play data on its own, so this is the easiest way to get stats, charts, and year-end recaps for what you play here."
    >
      {!status ? (
        <Skeleton className="h-10 w-full" />
      ) : status.connected ? (
        <div className="flex items-center justify-between rounded-md border border-border/50 bg-card/60 p-4">
          <div>
            <div className="text-sm font-semibold">Connected</div>
            <div className="text-xs text-muted-foreground">
              Scrobbling as <span className="text-foreground">{status.username}</span>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={disconnect}
            disabled={busy !== null}
          >
            {busy === "disconnect" && <Loader2 className="h-4 w-4 animate-spin" />}
            Disconnect
          </Button>
        </div>
      ) : status.has_credentials ? (
        <div className="flex flex-col gap-3 rounded-md border border-border/50 bg-card/60 p-4">
          <div className="text-sm">
            {status.using_default_credentials
              ? "Click Connect to authorize this app on your Last.fm account."
              : "API credentials saved. Click Connect to authorize this app on your Last.fm account."}
          </div>
          <div className="flex items-center gap-2">
            <Button
              onClick={startConnect}
              disabled={busy !== null}
              variant={pendingToken ? "outline" : "default"}
              size="sm"
            >
              {busy === "connect" && <Loader2 className="h-4 w-4 animate-spin" />}
              {pendingToken ? "Open Last.fm again" : "Connect"}
            </Button>
            {pendingToken && (
              <Button
                onClick={completeConnect}
                disabled={busy !== null}
                size="sm"
              >
                {busy === "complete" && <Loader2 className="h-4 w-4 animate-spin" />}
                I've approved — finish connecting
              </Button>
            )}
            {/* "Reset credentials" only makes sense when the user has
                entered their own. With baked-in defaults there's nothing
                personal stored here. */}
            {!status.using_default_credentials && (
              <Button
                variant="ghost"
                size="sm"
                onClick={disconnect}
                disabled={busy !== null}
              >
                Reset credentials
              </Button>
            )}
          </div>
          {pendingToken && (
            <p className="text-xs text-muted-foreground">
              Browser should have opened to Last.fm. Approve the app, then
              click "I've approved".
            </p>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-3 rounded-md border border-border/50 bg-card/60 p-4">
          <p className="text-sm text-muted-foreground">
            Paste your Last.fm API key and secret.{" "}
            <button
              onClick={openApiAccountPage}
              className="text-primary hover:underline"
            >
              Create a free API account
            </button>{" "}
            if you don't have one — any application name works, callback
            URL can be blank.
          </p>
          <Field label="API key">
            <Input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="e.g. 1a2b3c4d5e6f…"
              autoComplete="off"
            />
          </Field>
          <Field label="API secret">
            <Input
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              placeholder="e.g. 7g8h9i0j…"
              autoComplete="off"
              type="password"
            />
          </Field>
          <div>
            <Button
              size="sm"
              onClick={saveCreds}
              disabled={!apiKey.trim() || !apiSecret.trim() || busy !== null}
            >
              {busy === "save" && <Loader2 className="h-4 w-4 animate-spin" />}
              Save credentials
            </Button>
          </div>
        </div>
      )}
    </Section>
  );
}

function ShortcutRow({ keys, label }: { keys: string[]; label: string }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span>{label}</span>
      <div className="flex gap-1">
        {keys.map((k, i) => (
          <kbd
            key={i}
            className="min-w-[1.5rem] rounded border border-border bg-secondary px-1.5 py-0.5 text-center text-[11px] font-semibold text-foreground"
          >
            {k}
          </kbd>
        ))}
      </div>
    </div>
  );
}
