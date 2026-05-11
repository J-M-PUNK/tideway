import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  Bell,
  Bug,
  Check,
  ChevronRight,
  Code2,
  Download,
  ExternalLink,
  FileDown,
  Headphones,
  Import as ImportIcon,
  Info,
  Keyboard,
  Library as LibraryIcon,
  Loader2,
  LogOut,
  Moon,
  Music2,
  Palette,
  Power,
  Radio as RadioIcon,
  RefreshCw,
  Settings as SettingsIcon,
  Sun,
  Unlink,
} from "lucide-react";
import { api } from "@/api/client";
import type { QualityOption, Settings } from "@/api/types";
import { type AutoEqMode, type AutoEqState } from "@/hooks/useAutoEqState";
import {
  TEMPLATE_TOKENS,
  previewFilenameTemplateAsString,
} from "@/lib/filenameTemplate";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useToast } from "@/components/toast";
import { useDebouncedSettingsPut } from "@/hooks/useDebouncedSettingsPut";
import { useOfflineMode } from "@/hooks/useOfflineMode";
import {
  useUiPreferences,
  type StreamingQuality,
  type ThemeMode,
} from "@/hooks/useUiPreferences";
import { Skeleton } from "@/components/Skeletons";
import { cn } from "@/lib/utils";

type SaveStatus = "idle" | "saving" | "saved" | "error";

// Tab key persisted in sessionStorage so navigating away to another
// page and coming back keeps you on the same settings section. We
// don't put it in URL state because there's no use-case for deep-
// linking specific settings tabs from external sources, and the URL
// pollution would be visible whenever the user copies the address.
const TAB_STORAGE_KEY = "tideway:settings-tab";
const DEFAULT_TAB = "playback";

function readInitialTab(): string {
  try {
    return sessionStorage.getItem(TAB_STORAGE_KEY) || DEFAULT_TAB;
  } catch {
    return DEFAULT_TAB;
  }
}

export function SettingsPage({ onLogout }: { onLogout: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [qualities, setQualities] = useState<QualityOption[]>([]);
  const [status, setStatus] = useState<SaveStatus>("idle");
  const [tab, setTab] = useState<string>(readInitialTab);
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
        const [s, qs] = await Promise.all([
          api.settings.get(),
          api.qualities(),
        ]);
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
    if (saveTimerRef.current !== null)
      window.clearTimeout(saveTimerRef.current);
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
      if (saveTimerRef.current !== null)
        window.clearTimeout(saveTimerRef.current);
      if (savedIndicatorRef.current !== null)
        window.clearTimeout(savedIndicatorRef.current);
    };
  }, []);

  if (loadError)
    return (
      <div className="text-sm text-destructive">
        Couldn't load settings: {loadError.message}
      </div>
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

  const showAirPlay = import.meta.env.VITE_SHOW_AIRPLAY === "1";
  const onTabChange = (next: string) => {
    setTab(next);
    try {
      sessionStorage.setItem(TAB_STORAGE_KEY, next);
    } catch {
      // sessionStorage can throw in private-browsing contexts. The
      // tab still works for the current visit; we just lose the
      // sticky-across-navigation property.
    }
  };

  return (
    <div className="max-w-4xl">
      <h1 className="mb-8 flex items-center gap-3 text-3xl font-bold tracking-tight">
        <SettingsIcon className="h-7 w-7" /> Settings
      </h1>

      <Tabs
        orientation="vertical"
        value={tab}
        onValueChange={onTabChange}
        className="flex gap-8"
      >
        <TabsList className="sticky top-4 flex w-48 shrink-0 flex-col items-stretch gap-1 self-start">
          <SettingsTab value="playback" icon={Headphones} label="Playback" />
          <SettingsTab value="downloads" icon={Download} label="Downloads" />
          <SettingsTab value="library" icon={LibraryIcon} label="Library" />
          <SettingsTab value="import" icon={ImportIcon} label="Import" />
          <SettingsTab value="appearance" icon={Palette} label="Appearance" />
          <SettingsTab
            value="notifications"
            icon={Bell}
            label="Notifications"
          />
          <SettingsTab value="autostart" icon={Power} label="Launch on login" />
          <SettingsTab value="lastfm" icon={Music2} label="Last.fm" />
          <SettingsTab value="shortcuts" icon={Keyboard} label="Shortcuts" />
          {showAirPlay && (
            <SettingsTab value="airplay" icon={RadioIcon} label="AirPlay" />
          )}
          <SettingsTab value="about" icon={Info} label="About" />
        </TabsList>

        <div className="min-w-0 flex-1">
          <TabsContent value="playback" className="mt-0">
            <Section title="Playback" icon={Headphones}>
              <Field label="Streaming quality">
                <select
                  value={
                    // Clamp the stored value against the filtered list so a
                    // stale "hi_res_lossless" pref doesn't show an off-list
                    // value after a subscription downgrade.
                    qualities.some((q) => q.value === ui.streamingQuality)
                      ? ui.streamingQuality
                      : (qualities[0]?.value ?? "low_320k")
                  }
                  onChange={(e) =>
                    ui.set({
                      streamingQuality: e.target.value as StreamingQuality,
                    })
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
              <Field
                label="Explicit content"
                hint="Tidal returns both clean and explicit edits of the same album or track. Pick which copy you want to see when both exist."
              >
                <select
                  value={settings.explicit_content_preference}
                  onChange={(e) =>
                    patch({
                      explicit_content_preference: e.target
                        .value as Settings["explicit_content_preference"],
                    })
                  }
                  className="h-10 rounded-md border border-input bg-secondary px-3 text-sm"
                >
                  <option value="explicit">Show explicit</option>
                  <option value="clean">Show clean</option>
                  <option value="both">Show both</option>
                </select>
              </Field>
              <Toggle
                checked={settings.continue_playing_after_queue_ends}
                onChange={(v) =>
                  patch({ continue_playing_after_queue_ends: v })
                }
                label="Continue playing music after your queue ends"
                hint="When on, the player queues an Artist Radio mix seeded from the last track's primary artist so playback never stops on its own. When off, an album re-primes its first track paused (one tap of Play repeats the album); other queues just stop."
              />
            </Section>
          </TabsContent>

          {showAirPlay && (
            <TabsContent value="airplay" className="mt-0">
              <AirPlaySection />
            </TabsContent>
          )}

          <TabsContent value="downloads" className="mt-0">
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

              <Field label="Videos folder">
                <Input
                  value={settings.videos_dir}
                  onChange={(e) => patch({ videos_dir: e.target.value })}
                  placeholder="/path/to/videos"
                />
              </Field>

              <Field
                label="Filename template"
                hint={
                  <FilenameTemplateHint
                    template={settings.filename_template}
                    outputDir={settings.output_dir}
                    createAlbumFolders={settings.create_album_folders}
                  />
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
                hint="Only takes effect when the filename template doesn't already contain a folder separator (/). With a multi-segment template the template itself defines the folder structure."
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
                  onChange={(e) =>
                    patch({ concurrent_downloads: Number(e.target.value) })
                  }
                  className="h-2 w-full cursor-pointer appearance-none rounded-full bg-secondary accent-primary"
                  aria-label="Concurrent downloads"
                />
              </Field>
              <Field
                label={
                  settings.download_rate_limit_mbps > 0
                    ? `Download speed limit — ${settings.download_rate_limit_mbps} MB/s`
                    : "Download speed limit — unlimited"
                }
                hint="Capping your download rate makes the pattern look like aggressive prefetch instead of a scrape, the single most effective thing you can do to keep your Tidal account out of the anti-abuse bucket. Default 10 MB/s downloads a 4-minute Max track in about 4 seconds. Set to 0 for unlimited if you've accepted the ban risk."
              >
                <input
                  type="range"
                  min={0}
                  max={50}
                  step={5}
                  value={settings.download_rate_limit_mbps}
                  onChange={(e) =>
                    patch({ download_rate_limit_mbps: Number(e.target.value) })
                  }
                  className="h-2 w-full cursor-pointer appearance-none rounded-full bg-secondary accent-primary"
                  aria-label="Download speed limit"
                />
              </Field>
            </Section>
          </TabsContent>

          <TabsContent value="library" className="mt-0">
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
          </TabsContent>

          <TabsContent value="import" className="mt-0">
            <Section
              title="Import"
              icon={ImportIcon}
              description="Bring in playlists, liked songs, saved albums, and followed artists from Spotify, Deezer, or M3U / text files."
            >
              <Link
                to="/import"
                className="group flex items-center gap-3 rounded-md border border-border/50 bg-card/60 p-3 text-sm transition-colors hover:bg-accent/40"
              >
                <ImportIcon className="h-4 w-4 text-primary" />
                <div className="flex-1">
                  <div className="font-semibold">Open import hub</div>
                  <div className="text-xs text-muted-foreground">
                    Pick what you want to import and which specific items to
                    bring over.
                  </div>
                </div>
                <ChevronRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
              </Link>
              <Toggle
                checked={!ui.importLinkDismissed}
                onChange={(v) => ui.set({ importLinkDismissed: !v })}
                label="Show Import in the sidebar"
              />
            </Section>
          </TabsContent>

          <TabsContent value="appearance" className="mt-0">
            <Section title="Appearance" icon={Palette}>
              <Field label="Theme">
                <ThemePicker
                  value={ui.theme}
                  onChange={(t) => ui.set({ theme: t })}
                />
              </Field>
            </Section>
          </TabsContent>

          <TabsContent value="notifications" className="mt-0">
            <Section
              title="Notifications"
              icon={Bell}
              description="Native OS notifications. Track-change notifications only fire when the window is unfocused, so the in-app now-playing bar isn't duplicated by a bezel."
            >
              <Toggle
                checked={settings.notify_on_complete}
                onChange={(v) => patch({ notify_on_complete: v })}
                label="Notify me when downloads finish"
              />
              <Toggle
                checked={settings.notify_on_track_change}
                onChange={(v) => patch({ notify_on_track_change: v })}
                label="Notify me when the track changes (while the window is unfocused)"
              />
            </Section>
          </TabsContent>

          <TabsContent value="autostart" className="mt-0">
            <AutostartSection />
          </TabsContent>

          <TabsContent value="lastfm" className="mt-0">
            <LastFmSection />
          </TabsContent>

          <TabsContent value="shortcuts" className="mt-0">
            <Section
              title="Keyboard shortcuts"
              icon={Keyboard}
              description="Window must be focused for these to fire. Media keys (Play/Pause/Next/Prev) also work globally — the app listens for them even when minimized."
            >
              <ShortcutRow keys={["⌘", "K"]} label="Search / command palette" />
              <ShortcutRow keys={["⌘", ","]} label="Open Settings" />
              <ShortcutRow keys={["Space"]} label="Play / pause" />
              <ShortcutRow keys={["Shift", "→"]} label="Next track" />
              <ShortcutRow keys={["Shift", "←"]} label="Previous track" />
              <ShortcutRow keys={["↑"]} label="Volume up" />
              <ShortcutRow keys={["↓"]} label="Volume down" />
              <ShortcutRow keys={["M"]} label="Mute / unmute" />
              <ShortcutRow keys={["S"]} label="Toggle shuffle" />
              <ShortcutRow keys={["R"]} label="Cycle repeat" />
              <ShortcutRow keys={["L"]} label="Like / unlike current track" />
            </Section>
          </TabsContent>

          <TabsContent value="about" className="mt-0">
            <AboutSection />
          </TabsContent>
        </div>
      </Tabs>

      <div className="mt-8 flex items-center gap-3">
        <Button variant="outline" onClick={onLogout}>
          <LogOut className="h-4 w-4" /> Log out
        </Button>
        <SaveStatus status={status} />
      </div>
    </div>
  );
}

/**
 * Vertical-rail tab trigger. Wraps Radix's TabsTrigger but overrides
 * the default centered-pill style so the rail's items sit flush left
 * and span the rail width. The active state inverts to the foreground
 * tone so the selected tab stands out against the dark rail.
 */
function SettingsTab({
  value,
  icon: Icon,
  label,
}: {
  value: string;
  icon: typeof Bell;
  label: string;
}) {
  return (
    <TabsTrigger
      value={value}
      // Same left-edge primary indicator pattern as the sidebar
      // NavLinks. The `before:` pseudo-element is invisible by
      // default and grows to h-5 with primary tint when the tab is
      // active. The h-transition makes clicking between tabs read
      // as the indicator growing into the new row.
      className="!relative !justify-start !rounded-md !px-3 !py-2 text-left text-sm font-medium text-muted-foreground before:absolute before:left-0 before:top-1/2 before:h-0 before:w-0.5 before:-translate-y-1/2 before:rounded-r before:bg-primary before:opacity-0 before:transition-all before:duration-200 hover:!bg-accent/40 data-[state=active]:!bg-accent data-[state=active]:!text-foreground data-[state=active]:before:h-5 data-[state=active]:before:opacity-100"
    >
      <Icon className="mr-2 h-4 w-4 shrink-0" />
      {label}
    </TabsTrigger>
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
        {description && (
          <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>
        )}
      </div>
      {children}
    </section>
  );
}

function FilenameTemplateHint({
  template,
  outputDir,
  createAlbumFolders,
}: {
  template: string;
  outputDir: string;
  createAlbumFolders: boolean;
}) {
  // Live preview against a stable sample track so the user sees
  // what their template will produce regardless of what they're
  // currently browsing. `/` in the template creates folders.
  const preview = previewFilenameTemplateAsString(
    template,
    outputDir,
    createAlbumFolders,
  );
  return (
    <div className="flex flex-col gap-2">
      <div>
        Use <code>/</code> to nest folders. Available tokens:
      </div>
      <ul className="grid grid-cols-1 gap-x-4 gap-y-0.5 sm:grid-cols-2">
        {TEMPLATE_TOKENS.map(({ token, description }) => (
          <li key={token} className="leading-snug">
            <code>{token}</code>{" "}
            <span className="text-muted-foreground/80">— {description}</span>
          </li>
        ))}
      </ul>
      <div className="mt-1">
        <span className="text-muted-foreground/80">Preview:</span>{" "}
        <code className="break-all">{preview}</code>
      </div>
    </div>
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
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  /** Optional explanatory text rendered below the checkbox row. Same
   *  visual treatment as the hint on `<Field>`. */
  hint?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="flex items-center gap-3 text-sm">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="h-4 w-4 accent-primary"
        />
        {label}
      </label>
      {hint && <p className="ml-7 text-xs text-muted-foreground">{hint}</p>}
    </div>
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
 * Equalizer + audio-output device picker. Persisted in the backend
 * settings.json so a relaunch keeps the user's sound + device.
 *
 * Rendered as Fields (no outer Section) so they compose into the
 * parent Playback section alongside the streaming-quality picker.
 *
 * Preset dropdown picks one of the backend's built-ins and the
 * backend resolves to per-band amplitudes (so "Rock" renders the
 * matching slider curve immediately). Manual slider changes POST
 * the full band array with `preamp` = null ("no explicit preamp").
 */
function AudioEngineFields() {
  const toast = useToast();
  const [devices, setDevices] = useState<{
    devices: { id: string; name: string }[];
    current: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const d = await api.player.outputDevices();
        if (cancelled) return;
        setDevices(d);
      } catch {
        /* audio engine not available — fields stay hidden */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!devices) return null;

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

      {/* Order matches the audio path: ReplayGain → EQ → Crossfeed.
          The Signal Path readout shows stages in this same order, so
          the mental model "first in the panel = first in the chain"
          holds end-to-end. */}
      <ReplayGainField />
      <EqField />
      <CrossfeedField />
    </>
  );
}

/**
 * ReplayGain loudness leveling. Three modes (off / track / album)
 * + a preamp slider + a clipping-prevention toggle. Self-contained
 * on settings.get/put, same pattern as CrossfeedField.
 *
 * The mode picker sits at the top because it's the only control
 * that determines whether the rest of the section even matters —
 * with mode=off, preamp and clipping prevention are inert.
 */
function ReplayGainField() {
  const [state, setState] = useState<{
    mode: "off" | "track" | "album";
    preamp: number;
    preventClipping: boolean;
  } | null>(null);
  const debouncedPut = useDebouncedSettingsPut();

  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (cancelled) return;
        setState({
          mode: s.replaygain_mode ?? "off",
          preamp: s.replaygain_preamp_db ?? 0,
          preventClipping: s.replaygain_prevent_clipping ?? true,
        });
      })
      .catch(() => {
        if (!cancelled)
          setState({ mode: "off", preamp: 0, preventClipping: true });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!state) return null;

  const setMode = (mode: "off" | "track" | "album") => {
    setState({ ...state, mode });
    debouncedPut({ replaygain_mode: mode });
  };
  const setPreamp = (preamp: number) => {
    setState({ ...state, preamp });
    debouncedPut({ replaygain_preamp_db: preamp });
  };
  const setPreventClipping = (preventClipping: boolean) => {
    setState({ ...state, preventClipping });
    debouncedPut({ replaygain_prevent_clipping: preventClipping });
  };

  return (
    <Field
      label="ReplayGain"
      hint="Loudness leveling — Tidal masters carry per-track and per-album gain offsets relative to the EBU R128 reference. Track mode is best for shuffle; album mode preserves the artist's intended loudness relationships within an album. Off keeps the audio path bit-perfect."
    >
      <div className="flex flex-col gap-3">
        <select
          value={state.mode}
          onChange={(e) => setMode(e.target.value as "off" | "track" | "album")}
          className="h-10 rounded-md border border-input bg-secondary px-3 text-sm"
        >
          <option value="off">Off — bit-perfect</option>
          <option value="track">Track — per-track gain</option>
          <option value="album">Album — album-wide gain</option>
        </select>
        {state.mode !== "off" && (
          <>
            <div>
              <div className="mb-1 text-xs text-muted-foreground">
                {state.preamp >= 0 ? "+" : ""}
                {state.preamp.toFixed(1)} dB preamp offset
              </div>
              <input
                type="range"
                min={-10}
                max={10}
                step={0.5}
                value={state.preamp}
                onChange={(e) => setPreamp(Number(e.target.value))}
                className="h-2 w-full cursor-pointer appearance-none rounded-full bg-secondary accent-primary"
                aria-label="ReplayGain preamp offset"
              />
            </div>
            <Toggle
              checked={state.preventClipping}
              onChange={setPreventClipping}
              label="Prevent clipping"
              hint="Clamp the applied gain so peak * gain ≤ 1.0. Off lets you push past the limit at your own risk for masters that won't actually clip."
            />
          </>
        )}
      </div>
    </Field>
  );
}

/**
 * Bauer-style crossfeed slider. Off (0) by default — user opts in.
 * 20-40 % is the typical taste range; below that the effect isn't
 * audible, above it the centre image pulls too far toward mono.
 *
 * Disclosure-style hint explains what crossfeed actually does, since
 * it's a less-common term than "EQ" or "exclusive mode" — the
 * audiophile crowd will recognise it but new users won't.
 *
 * Self-contained on settings.get/put rather than threading
 * settings/patch in from the parent — matches how the sibling
 * EqField fetches its own state, keeps the component composable.
 */
function CrossfeedField() {
  const [amount, setAmount] = useState<number | null>(null);
  const debouncedPut = useDebouncedSettingsPut();

  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (!cancelled) setAmount(s.crossfeed_amount ?? 0);
      })
      .catch(() => {
        if (!cancelled) setAmount(0);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (amount === null) return null;

  const label = amount > 0 ? `Crossfeed — ${amount}%` : "Crossfeed — off";

  const onChange = (next: number) => {
    setAmount(next);
    debouncedPut({ crossfeed_amount: next });
  };

  return (
    <Field
      label={label}
      hint="Headphone-only stereo imaging. Bleeds the low frequencies of each channel into the opposite ear so hard-panned mixes (60s/70s rock, jazz, orchestral) don't sound like they're playing inside your skull. Highs stay channel-isolated. 20-40% is a tasteful range; 0 disables and audio runs bit-perfect again."
    >
      <input
        type="range"
        min={0}
        max={100}
        step={5}
        value={amount}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-2 w-full cursor-pointer appearance-none rounded-full bg-secondary accent-primary"
        aria-label="Crossfeed amount"
      />
    </Field>
  );
}

/**
 * The whole EQ section as one Field with a single mode picker.
 *
 * The previous layout had three separate sections — Headphone
 * profile (mode picker), Manual EQ (always-rendered sliders),
 * Per-device profiles (a button) — and users couldn't tell which
 * combination of them was actually doing the EQ work. Sliders
 * dimmed themselves when not in manual mode, but they were still
 * visually present, suggesting they did something. The mode
 * picker said "Off" / "Manual" / "Profile" but those words also
 * appeared in the manual-EQ enable hint.
 *
 * Now: one decision, one set of controls. The mode picker decides
 * what kind of EQ runs. Whatever's appropriate for that mode
 * renders below the picker; nothing else is on screen.
 *
 *   Off:     "Audio plays bit-perfect, no EQ stage."
 *   Manual:  10-band sliders + presets + reset.
 *   Profile: active-profile card, headphone catalog button, tone
 *            adjuster button, per-device mapping button. The
 *            mapping button's only useful when the user has
 *            profiles to map to, so it lives inside this branch
 *            instead of as a top-level affordance.
 */
function EqField() {
  const toast = useToast();
  const [state, setState] = useState<AutoEqState | null>(null);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [toneOpen, setToneOpen] = useState(false);
  const [deviceMappingOpen, setDeviceMappingOpen] = useState(false);

  // Per-device mappings only make sense when the user has more
  // than one output device (or has explicitly mapped at least one
  // already). Single-DAC users would just see noise. We poll the
  // device list on mount and refresh when the dialog closes so a
  // newly-plugged DAC shows up without an app restart.
  const [deviceCount, setDeviceCount] = useState(0);
  const [mappedCount, setMappedCount] = useState(0);

  // True only across the very first refresh, so the stale-profile
  // toast fires once at mount and not again when the user switches
  // modes or imports a new profile.
  const firstLoadRef = useRef(true);

  const refresh = async () => {
    try {
      const s = await api.player.autoEqState();
      // Stale-profile detection: user was in profile mode with a
      // previously-loaded headphone, but the profile id no longer
      // resolves to anything on disk (typical after upgrading from
      // a Tideway version that bundled a profile we've since
      // removed, or after the user deleted the imported profile by
      // hand on the filesystem). Server's bootstrap silently
      // declines to apply the missing profile — the audio is
      // bit-perfect, but the user has no idea why their EQ
      // disappeared. Surface it once.
      if (
        firstLoadRef.current &&
        s.mode === "profile" &&
        s.active_profile_id &&
        s.active_profile === null
      ) {
        toast.show({
          kind: "info",
          title: "AutoEQ profile missing",
          description: `“${s.active_profile_id}” is no longer installed. Pick or import a profile in Settings.`,
        });
      }
      firstLoadRef.current = false;
      setState(s);
    } catch {
      /* feature not available — keep section hidden */
    }
  };

  const refreshDevices = async () => {
    try {
      const d = await api.player.autoEqDevices();
      setDeviceCount(d.devices.length);
      setMappedCount(
        d.devices.filter((dev) => dev.mapped_profile_id !== null).length,
      );
    } catch {
      /* fine — section just won't surface the per-device button */
    }
  };

  useEffect(() => {
    void refresh();
    void refreshDevices();
  }, []);

  if (state === null) return null;

  const switchMode = async (mode: AutoEqMode) => {
    const prev = state;
    setState({ ...state, mode });
    try {
      await api.player.autoEqSetMode(mode);
    } catch (err) {
      setState(prev);
      toast.show({
        kind: "error",
        title: "Couldn't switch EQ mode",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const toggleBypass = async () => {
    if (state.active_profile === null) return;
    const next = !state.bypass;
    setState({ ...state, bypass: next });
    try {
      await api.player.autoEqSetBypass(next);
    } catch (err) {
      setState({ ...state, bypass: !next });
      toast.show({
        kind: "error",
        title: "Couldn't toggle bypass",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const hasActive = state.active_profile !== null;
  const hasProfiles = state.profile_catalog_size > 0;
  // Per-device profiles is a power-user surface — only show it
  // when there's actually something to map (>1 known output
  // device, or at least one already mapped).
  const showPerDeviceButton = deviceCount > 1 || mappedCount > 0;

  return (
    <Field label="Equalizer">
      <div className="flex flex-col gap-3">
        <ModeCard
          mode="off"
          active={state.mode === "off"}
          label="Off"
          description="Bypass the EQ stage. Audio plays bit-perfect."
          onSelect={() => switchMode("off")}
        />
        <ModeCard
          mode="manual"
          active={state.mode === "manual"}
          label="Manual"
          description="10-band parametric EQ. Drag sliders or pick a preset."
          onSelect={() => switchMode("manual")}
        />
        <ModeCard
          mode="profile"
          active={state.mode === "profile"}
          label="Profile"
          description="AutoEQ correction tuned to your specific headphones. Imported from autoeq.app."
          onSelect={() => switchMode("profile")}
        />

        {state.mode === "manual" && (
          <div className="mt-2">
            <ManualEqSliders />
          </div>
        )}

        {state.mode === "profile" && !hasProfiles && (
          <div className="mt-2 flex flex-col items-start gap-3 rounded-md border border-primary/30 bg-primary/5 p-4">
            <div className="text-sm font-semibold">No profiles yet.</div>
            <p className="text-xs text-muted-foreground">
              Tideway no longer ships starter profiles. Generate one for your
              headphones at{" "}
              <a
                href="https://autoeq.app"
                target="_blank"
                rel="noopener noreferrer"
                className="underline hover:text-foreground"
              >
                autoeq.app
              </a>{" "}
              (download as{" "}
              <span className="font-mono">EqualizerAPO Parametric Eq</span> or{" "}
              <span className="font-mono">Custom Parametric Eq</span>) and
              import the file here.
            </p>
            <button
              type="button"
              onClick={() => setCatalogOpen(true)}
              className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/80"
            >
              Import your first profile
            </button>
          </div>
        )}

        {state.mode === "profile" && hasProfiles && (
          <div className="mt-2 flex flex-col gap-2">
            {hasActive && state.active_profile && (
              <button
                type="button"
                onClick={() => setCatalogOpen(true)}
                className="flex items-start justify-between gap-3 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-left text-xs transition-colors hover:bg-primary/10"
                title="Click to change profile"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-primary">
                    Active profile
                  </div>
                  <div className="mt-0.5 truncate text-sm font-semibold">
                    {state.active_profile.brand} {state.active_profile.model}
                  </div>
                  <div className="truncate text-muted-foreground">
                    {state.active_profile.source} ·{" "}
                    {state.active_profile.band_count} bands · preamp{" "}
                    {state.active_profile.preamp_db.toFixed(1)} dB
                  </div>
                </div>
                <span className="flex-shrink-0 self-center rounded border border-primary/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
                  Change
                </span>
              </button>
            )}

            <div className="flex flex-wrap gap-2">
              {!hasActive && (
                <button
                  type="button"
                  onClick={() => setCatalogOpen(true)}
                  className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/80"
                >
                  Pick a profile
                </button>
              )}
              {hasActive && (
                <>
                  <button
                    type="button"
                    onClick={() => setToneOpen(true)}
                    className="rounded-md border border-input bg-secondary px-3 py-1.5 text-xs font-semibold hover:bg-accent"
                    title="Tilt sliders + frequency-response graph"
                  >
                    Tilt &amp; graph
                  </button>
                  <button
                    type="button"
                    onClick={() => void toggleBypass()}
                    className={cn(
                      "rounded-md border px-3 py-1.5 text-xs font-semibold transition-colors",
                      state.bypass
                        ? "border-amber-500/40 bg-amber-500/10 text-amber-500 hover:bg-amber-500/20"
                        : "border-input bg-secondary text-foreground hover:bg-accent",
                    )}
                    title="A/B compare with the EQ off without losing your selection."
                  >
                    {state.bypass ? "Bypassed (A/B)" : "Bypass A/B"}
                  </button>
                </>
              )}
              <button
                type="button"
                onClick={() => setCatalogOpen(true)}
                className="rounded-md border border-input bg-secondary px-3 py-1.5 text-xs font-semibold hover:bg-accent"
              >
                Manage profiles
              </button>
              {showPerDeviceButton && (
                <button
                  type="button"
                  onClick={() => setDeviceMappingOpen(true)}
                  className="rounded-md border border-input bg-secondary px-3 py-1.5 text-xs font-semibold hover:bg-accent"
                  title="Map output devices to specific profiles so plugging in a different DAC swaps the EQ automatically."
                >
                  Per-device profiles
                  {mappedCount > 0 && (
                    <span className="ml-1 text-muted-foreground">
                      ({mappedCount})
                    </span>
                  )}
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      <ProfilesDialog
        open={catalogOpen}
        onOpenChange={setCatalogOpen}
        activeProfileId={state.active_profile_id}
        onChanged={() => {
          void refresh();
        }}
      />
      <ToneDialog
        open={toneOpen}
        onOpenChange={setToneOpen}
        activeProfileId={state.active_profile_id}
        tilt={state.tilt}
        onTiltChange={(t) =>
          setState((prev) => (prev ? { ...prev, tilt: t } : prev))
        }
      />
      <Dialog
        open={deviceMappingOpen}
        onOpenChange={(open) => {
          setDeviceMappingOpen(open);
          // Refresh device list when the dialog closes so the
          // mapped-count badge stays accurate.
          if (!open) void refreshDevices();
        }}
      >
        <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Per-device profiles</DialogTitle>
            <DialogDescription>
              Tideway can pick a different AutoEQ profile depending on which
              output device is active. Useful if you switch between IEMs on a
              portable DAC and over-ears on a desktop amp.
            </DialogDescription>
          </DialogHeader>
          <AutoEqDeviceMappingField />
        </DialogContent>
      </Dialog>
    </Field>
  );
}

/**
 * One row in the EQ mode picker. Replaces the old pill buttons —
 * each mode now has a label AND a one-line description so a first-
 * time user can read what each option does without hovering for a
 * tooltip.
 */
function ModeCard({
  active,
  label,
  description,
  onSelect,
}: {
  mode: AutoEqMode;
  active: boolean;
  label: string;
  description: string;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex items-start gap-3 rounded-md border px-3 py-2 text-left transition-colors",
        active
          ? "border-primary bg-primary/10"
          : "border-input bg-secondary hover:bg-accent",
      )}
    >
      <div
        className={cn(
          "mt-0.5 h-3.5 w-3.5 flex-shrink-0 rounded-full border-2 transition-colors",
          active ? "border-primary bg-primary" : "border-muted-foreground/40",
        )}
      />
      <div className="min-w-0 flex-1">
        <div
          className={cn(
            "text-xs font-semibold",
            active ? "text-primary" : "text-foreground",
          )}
        >
          {label}
        </div>
        <div className="text-[11px] text-muted-foreground">{description}</div>
      </div>
    </button>
  );
}

/**
 * 10-band parametric EQ surface. Self-contained: fetches its own
 * state from /api/player/eq, renders the preset dropdown + sliders,
 * commits changes on slider release. Only mounted when EQ mode is
 * "manual" — outside that mode the UI doesn't render at all, so
 * there's no risk of confusing "are these sliders doing anything?"
 * questions.
 */
function ManualEqSliders() {
  const toast = useToast();
  const [eq, setEq] = useState<{
    enabled: boolean;
    bands: number[];
    preamp: number | null;
    band_count: number;
    frequencies: number[];
    presets: { index: number; name: string; bands: number[] }[];
  } | null>(null);
  const [localBands, setLocalBands] = useState<number[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const e = await api.player.eq();
        if (cancelled) return;
        setEq(e);
      } catch {
        /* engine not available */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (eq)
      setLocalBands(
        eq.bands.length ? eq.bands : new Array(eq.band_count).fill(0),
      );
  }, [eq]);

  if (!eq || localBands === null) return null;

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

  // Match a preset against current sliders so the active preset's
  // card highlights as "selected." Compare with a small epsilon
  // because sliders snap to 0.5 dB but presets are integer dB.
  const activePresetIdx = (() => {
    for (const p of eq.presets) {
      if (p.bands.length !== localBands.length) continue;
      let same = true;
      for (let i = 0; i < p.bands.length; i++) {
        if (Math.abs(p.bands[i] - localBands[i]) > 0.05) {
          same = false;
          break;
        }
      }
      if (same) return p.index;
    }
    return null;
  })();

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Presets
        </div>
        <Button size="sm" variant="outline" onClick={reset}>
          Reset
        </Button>
      </div>

      {/* Horizontal scroll of preset cards. Each card shows the
          name plus a tiny SVG of the band gains, so the user can
          eyeball "this one boosts bass and treble" without applying
          and reverting. Click to apply. */}
      <div className="flex gap-2 overflow-x-auto pb-1">
        {eq.presets.map((p) => (
          <PresetCard
            key={p.index}
            name={p.name}
            bands={p.bands}
            active={activePresetIdx === p.index}
            onClick={() => pickPreset(p.index)}
          />
        ))}
      </div>

      <div className="flex items-end gap-3">
        {localBands.map((v, i) => (
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
  );
}

/**
 * One preset in the manual-EQ preset row. Renders the name plus a
 * tiny bar chart of the preset's band gains — green bars for boost
 * (positive gain), red for cut, neutral for flat. Lets the user
 * recognise "Bass Boost" at a glance because the leftmost bars
 * are taller, instead of having to apply and revert to discover
 * what each name actually does.
 *
 * SVG dimensions are deliberately small (90x32 ish); the row is
 * horizontally scrollable so adding more presets doesn't break
 * the Settings page width.
 */
function PresetCard({
  name,
  bands,
  active,
  onClick,
}: {
  name: string;
  bands: number[];
  active: boolean;
  onClick: () => void;
}) {
  const w = 80;
  const h = 28;
  const barGap = 1;
  const barW =
    bands.length > 0 ? (w - (bands.length - 1) * barGap) / bands.length : 0;
  // Bands clamp to ±12 dB at the slider level. Use that as the
  // chart's vertical range so a "Bass Boost +6" preset renders as
  // bars half the chart height — predictable scale across presets.
  const maxAbs = 12;

  return (
    <button
      type="button"
      onClick={onClick}
      title={`Apply preset "${name}"`}
      className={cn(
        "flex flex-shrink-0 flex-col items-center gap-1 rounded-md border px-2 py-1.5 transition-colors",
        active
          ? "border-primary bg-primary/10"
          : "border-input bg-secondary hover:bg-accent",
      )}
    >
      <svg
        width={w}
        height={h}
        viewBox={`0 0 ${w} ${h}`}
        className="overflow-visible"
      >
        {/* Zero-line so the user reads boost vs cut at a glance. */}
        <line
          x1={0}
          y1={h / 2}
          x2={w}
          y2={h / 2}
          stroke="currentColor"
          strokeOpacity={0.2}
          strokeWidth={1}
        />
        {bands.map((g, i) => {
          const norm = Math.max(-1, Math.min(1, g / maxAbs));
          const barH = (Math.abs(norm) * h) / 2;
          const x = i * (barW + barGap);
          const y = norm >= 0 ? h / 2 - barH : h / 2;
          // Green-ish for boost, red-ish for cut, muted for flat.
          // Using rgb literals because Tailwind's `fill-emerald-500`
          // etc. aren't reachable from inline SVG without arbitrary
          // class plumbing — direct values are simpler and stable.
          const color =
            Math.abs(g) < 0.1
              ? "rgb(120, 120, 120)"
              : g > 0
                ? "rgb(74, 222, 128)"
                : "rgb(248, 113, 113)";
          return (
            <rect
              key={i}
              x={x}
              y={y}
              width={barW}
              height={Math.max(barH, 0.5)}
              fill={color}
              opacity={0.85}
            />
          );
        })}
      </svg>
      <div
        className={cn(
          "max-w-[80px] truncate text-[10px] font-medium",
          active ? "text-primary" : "text-foreground/80",
        )}
      >
        {name}
      </div>
    </button>
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
 * AirPlay output control. Lists discovered receivers on the local
 * network, walks the user through a one-time pair, and lets them
 * connect / disconnect. Audio that the PCMPlayer produces is tee'd
 * to the connected device via pyatv; local speakers keep playing
 * in parallel (mute with the volume slider if you don't want echo).
 *
 * Not end-to-end tested against hardware yet. Pair flow is based
 * on pyatv's documented API; first real test will surface any
 * protocol quirks on HomePods, AirPlay speakers, or Apple TVs.
 */

/**
 * Headphone-profile section — Phase 2 of the AutoEQ work.
 *
 * Shows a mode toggle (Off / Manual / Profile) and, when in
 * profile mode, a search input + result list. Manual mode keeps
 * the existing 10-band sliders below; the two modes coexist via
 * the backend `eq_mode` setting.
 *
 * Search is server-side (the backend has rapidfuzz when
 * available; falls back to substring match). We re-fetch on
 * every keystroke after a 200ms debounce — the catalog is small
 * enough (~7 profiles in v1, ~5,000 once Phase 7 ships) that the
 * cost is negligible.
 */
// Types moved to @/hooks/useAutoEqState (single source of truth).
// This block previously had a parallel definition that drifted —
// it lacked the `bypass` field added in Phase 4, for instance.

/**
 * Dialog that lists installed profiles and lets the user import,
 * use, or delete them. Replaces the previous AutoEQ catalog
 * search — Tideway no longer browses AutoEQ's 5,000-profile
 * upstream from inside the app. Users grab the PEQ.txt files
 * they want from autoeq.app (or anywhere else that publishes
 * AutoEQ-format profiles) and import them here.
 *
 * Bundled profiles ship as a starter set so the picker isn't
 * empty out of the box, but they can't be deleted (they live
 * alongside the source code; reinstalling brings them back).
 * User-imported profiles get a Delete button.
 */
function ProfilesDialog({
  open,
  onOpenChange,
  activeProfileId,
  onChanged,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  activeProfileId: string;
  onChanged: () => void;
}) {
  const toast = useToast();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [profiles, setProfiles] = useState<
    {
      id: string;
      brand: string;
      model: string;
      source: string;
      preamp_db: number;
      band_count: number;
    }[]
  >([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const r = await api.player.autoEqList("", 500);
      setProfiles(r.profiles);
    } catch {
      setProfiles([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void refresh();
  }, [open]);

  const handleUse = async (id: string) => {
    setBusyId(id);
    try {
      await api.player.autoEqLoadProfile(id);
      onChanged();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't load profile",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!window.confirm(`Delete "${name}"? This can't be undone.`)) return;
    setBusyId(id);
    try {
      await api.player.autoEqDeleteProfile(id);
      toast.show({ kind: "success", title: "Deleted", description: name });
      void refresh();
      onChanged();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't delete",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  };

  const handleImport = async (file: File) => {
    setImporting(true);
    try {
      const text = await file.text();
      // Default-name from filename: "Sennheiser HD 600 ParametricEQ.txt"
      // -> "Sennheiser HD 600". User can rename when prompted.
      const baseName = file.name
        .replace(/\.txt$/i, "")
        .replace(/\s+ParametricEQ$/i, "")
        .trim();
      const headphoneName =
        window.prompt("Save this profile under what name?", baseName)?.trim() ||
        "";
      if (!headphoneName) return;
      try {
        const r = await api.player.autoEqImportProfile(headphoneName, text);
        toast.show({
          kind: "success",
          title: "Imported",
          description: r.headphone,
        });
        // Auto-load the imported profile so the user immediately
        // hears the correction.
        await api.player.autoEqLoadProfile(r.profile_id);
        onChanged();
        void refresh();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        if (msg.startsWith("409:")) {
          if (
            window.confirm(
              `A profile named "${headphoneName}" already exists. Replace it?`,
            )
          ) {
            const r = await api.player.autoEqImportProfile(
              headphoneName,
              text,
              true,
            );
            toast.show({
              kind: "success",
              title: "Replaced",
              description: r.headphone,
            });
            await api.player.autoEqLoadProfile(r.profile_id);
            onChanged();
            void refresh();
          }
        } else {
          throw err;
        }
      }
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't import profile",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setImporting(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Headphone profiles</DialogTitle>
          <DialogDescription>
            Pick which AutoEQ profile is active, or import a new one. Get
            profiles from{" "}
            <a
              href="https://autoeq.app"
              target="_blank"
              rel="noopener noreferrer"
              className="underline hover:text-foreground"
            >
              autoeq.app
            </a>
            : pick your headphones, choose a target, then under{" "}
            <strong>Download</strong> select{" "}
            <span className="font-mono">EqualizerAPO Parametric Eq</span> or{" "}
            <span className="font-mono">Custom Parametric Eq</span>. Both import
            as-is. Convolution, Graphic EQ (10-band / 31-band), and Wavelet
            exports aren't parametric and won't work.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <input
            ref={fileInputRef}
            type="file"
            accept=".txt,text/plain"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleImport(f);
            }}
            className="hidden"
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={importing}
            className="self-start rounded-md border border-primary bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/80 disabled:opacity-50"
          >
            {importing ? "Importing…" : "Import a profile…"}
          </button>

          {loading && profiles.length === 0 && (
            <div className="px-1 py-2 text-xs text-muted-foreground">
              Loading profiles…
            </div>
          )}
          {!loading && profiles.length === 0 && (
            <div className="rounded-md border border-input bg-secondary/40 px-3 py-3 text-xs text-muted-foreground">
              No profiles yet. Click "Import a profile…" to add one.
            </div>
          )}
          {profiles.length > 0 && (
            <div className="max-h-96 overflow-y-auto rounded-md border border-input">
              {profiles.map((p) => {
                const isActive = p.id === activeProfileId;
                const isImported = p.source === "User imported";
                return (
                  <div
                    key={p.id}
                    className={cn(
                      "flex items-center justify-between gap-3 border-b border-input/40 px-3 py-2 text-xs last:border-b-0",
                      isActive && "bg-primary/5",
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-semibold">
                        {p.brand} {p.model}
                      </div>
                      <div className="text-muted-foreground">
                        {p.source} · {p.band_count} bands · preamp{" "}
                        {p.preamp_db.toFixed(1)} dB
                      </div>
                    </div>
                    <div className="flex flex-shrink-0 items-center gap-1.5">
                      {isActive ? (
                        <span className="rounded bg-primary/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
                          In use
                        </span>
                      ) : (
                        <button
                          type="button"
                          onClick={() => handleUse(p.id)}
                          disabled={busyId === p.id}
                          className="rounded border border-primary bg-primary px-2 py-0.5 text-[10px] font-semibold text-primary-foreground hover:bg-primary/80 disabled:opacity-50"
                        >
                          {busyId === p.id ? "…" : "Use"}
                        </button>
                      )}
                      {isImported && (
                        <button
                          type="button"
                          onClick={() =>
                            handleDelete(p.id, `${p.brand} ${p.model}`)
                          }
                          disabled={busyId === p.id}
                          className="rounded border border-input px-2 py-0.5 text-[10px] font-semibold text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Tilt sliders + frequency-response graph in one dialog. They
 * belong together: tone adjustments are most intuitive when the
 * user can watch the FR curve respond to slider drags. Putting
 * them in separate dialogs would force the user to imagine the
 * effect, which defeats the point of having a graph.
 */
function ToneDialog({
  open,
  onOpenChange,
  activeProfileId,
  tilt,
  onTiltChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  activeProfileId: string;
  tilt: AutoEqState["tilt"];
  onTiltChange: (next: AutoEqState["tilt"]) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Adjust tone</DialogTitle>
          <DialogDescription>
            Tilt sliders stack on top of the active profile. The graph below
            shows what they do — raw measurement, target curve, and predicted
            post-EQ response.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <AutoEqResponseGraph activeProfileId={activeProfileId} tilt={tilt} />
          <AutoEqTiltSliders initial={tilt} onChange={onTiltChange} />
        </div>
      </DialogContent>
    </Dialog>
  );
}

type AutoEqResponseData = {
  frequencies_hz: number[];
  raw_db: number[] | null;
  target_db: number[] | null;
  post_eq_db: number[];
  sample_rate_hz: number;
  has_measurement: boolean;
};

function AutoEqResponseGraph({
  activeProfileId,
  tilt,
}: {
  activeProfileId: string;
  tilt: { preamp_offset_db: number; bass_db: number; treble_db: number };
}) {
  const [data, setData] = useState<AutoEqResponseData | null>(null);

  // Refetch on profile/tilt changes. Debounce so dragging a
  // slider doesn't fire 60 requests per second.
  useEffect(() => {
    if (!activeProfileId) {
      setData(null);
      return;
    }
    const handle = window.setTimeout(async () => {
      try {
        const r = await api.player.autoEqResponse(384);
        setData(r);
      } catch {
        setData(null);
      }
    }, 80);
    return () => window.clearTimeout(handle);
  }, [activeProfileId, tilt.preamp_offset_db, tilt.bass_db, tilt.treble_db]);

  if (data === null) return null;

  const W = 480;
  const H = 180;
  const PAD_L = 32;
  const PAD_R = 8;
  const PAD_T = 8;
  const PAD_B = 22;
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;

  // dB axis: pick a tight range that fits all three curves.
  const allValues: number[] = [...data.post_eq_db];
  if (data.raw_db) allValues.push(...data.raw_db);
  if (data.target_db) allValues.push(...data.target_db);
  let yMin = Math.min(...allValues);
  let yMax = Math.max(...allValues);
  // Pad and round outward to the nearest 5 dB so axis ticks are
  // sensible.
  yMin = Math.floor((yMin - 1) / 5) * 5;
  yMax = Math.ceil((yMax + 1) / 5) * 5;
  if (yMax - yMin < 10) yMax = yMin + 10;

  const xLogMin = Math.log10(data.frequencies_hz[0]);
  const xLogMax = Math.log10(
    data.frequencies_hz[data.frequencies_hz.length - 1],
  );
  const xLogRange = xLogMax - xLogMin;

  const px = (f: number) =>
    PAD_L + ((Math.log10(f) - xLogMin) / xLogRange) * innerW;
  const py = (db: number) => PAD_T + (1 - (db - yMin) / (yMax - yMin)) * innerH;

  const linePath = (values: number[]) =>
    values
      .map(
        (v, i) =>
          `${i === 0 ? "M" : "L"} ${px(data.frequencies_hz[i]).toFixed(1)} ${py(v).toFixed(1)}`,
      )
      .join(" ");

  const xTicks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10_000, 20_000];
  const yTickStep = yMax - yMin >= 30 ? 10 : 5;
  const yTicks: number[] = [];
  for (let v = yMin; v <= yMax; v += yTickStep) yTicks.push(v);

  return (
    <div className="rounded-md border border-input bg-secondary/40 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          Frequency response
        </div>
        <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
          {data.has_measurement && (
            <>
              <span className="flex items-center gap-1">
                <span className="inline-block h-2 w-3 rounded-sm bg-foreground/35" />
                Raw
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-px w-3 border-t border-dashed border-foreground/60" />
                Target
              </span>
            </>
          )}
          <span className="flex items-center gap-1">
            <span className="inline-block h-1 w-3 rounded-sm bg-primary" />
            Post-EQ
          </span>
        </div>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-44 w-full"
        preserveAspectRatio="none"
      >
        {yTicks.map((v) => {
          const y = py(v);
          return (
            <g key={`y${v}`}>
              <line
                x1={PAD_L}
                x2={W - PAD_R}
                y1={y}
                y2={y}
                stroke="currentColor"
                strokeOpacity={v === 0 ? 0.4 : 0.1}
                strokeWidth={v === 0 ? 1 : 0.5}
              />
              <text
                x={PAD_L - 4}
                y={y + 3}
                textAnchor="end"
                className="fill-current text-[9px] text-muted-foreground"
              >
                {v > 0 ? `+${v}` : v}
              </text>
            </g>
          );
        })}
        {xTicks.map((f) => {
          const x = px(f);
          if (x < PAD_L || x > W - PAD_R) return null;
          const label = f >= 1000 ? `${f / 1000}k` : `${f}`;
          return (
            <g key={`x${f}`}>
              <line
                x1={x}
                x2={x}
                y1={PAD_T}
                y2={H - PAD_B}
                stroke="currentColor"
                strokeOpacity={0.06}
                strokeWidth={0.5}
              />
              <text
                x={x}
                y={H - PAD_B + 12}
                textAnchor="middle"
                className="fill-current text-[9px] text-muted-foreground"
              >
                {label}
              </text>
            </g>
          );
        })}
        {data.raw_db && (
          <path
            d={linePath(data.raw_db)}
            fill="none"
            stroke="currentColor"
            strokeOpacity={0.35}
            strokeWidth={1}
          />
        )}
        {data.target_db && (
          <path
            d={linePath(data.target_db)}
            fill="none"
            stroke="currentColor"
            strokeOpacity={0.55}
            strokeWidth={1}
            strokeDasharray="3 3"
          />
        )}
        <path
          d={linePath(data.post_eq_db)}
          fill="none"
          stroke="hsl(var(--primary))"
          strokeWidth={1.6}
        />
      </svg>
      {!data.has_measurement && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          No measurement CSV bundled for this profile — showing EQ response
          only.
        </div>
      )}
    </div>
  );
}

/**
 * Tilt sliders below the active profile card. Three sliders
 * (-12..+12 dB): preamp offset, bass, treble. Live local state
 * for smooth dragging; commits to the server on slider release
 * via api.player.autoEqSetTilt. A reset button zeros everything.
 *
 * Tilt is user-global, not per-device — taste preference travels
 * with the listener. The backend persists in `eq_tilt_*` settings
 * fields so a relaunch keeps the user's curve.
 */
function AutoEqTiltSliders({
  initial,
  onChange,
}: {
  initial: { preamp_offset_db: number; bass_db: number; treble_db: number };
  onChange: (next: {
    preamp_offset_db: number;
    bass_db: number;
    treble_db: number;
  }) => void;
}) {
  const toast = useToast();
  const [local, setLocal] = useState(initial);

  // Re-sync from props when the parent state refreshes (e.g.
  // after picking a different profile, the server still returns
  // the same tilt values but the parent re-renders us).
  useEffect(() => {
    setLocal(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial.preamp_offset_db, initial.bass_db, initial.treble_db]);

  const commit = async (
    field: "preamp_offset_db" | "bass_db" | "treble_db",
    value: number,
  ) => {
    const next = { ...local, [field]: value };
    setLocal(next);
    onChange(next);
    try {
      await api.player.autoEqSetTilt({ [field]: value });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't update tilt",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const reset = async () => {
    const zero = { preamp_offset_db: 0, bass_db: 0, treble_db: 0 };
    setLocal(zero);
    onChange(zero);
    try {
      await api.player.autoEqSetTilt(zero);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't reset tilt",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <div className="rounded-md border border-input bg-secondary/40 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          Tilt
        </div>
        <button
          type="button"
          onClick={reset}
          className="rounded-md border border-input px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent"
        >
          Reset
        </button>
      </div>
      <div className="flex flex-col gap-2">
        <TiltSlider
          label="Preamp offset"
          value={local.preamp_offset_db}
          onChange={(v) =>
            setLocal((prev) => ({ ...prev, preamp_offset_db: v }))
          }
          onCommit={(v) => commit("preamp_offset_db", v)}
        />
        <TiltSlider
          label="Bass (80 Hz shelf)"
          value={local.bass_db}
          onChange={(v) => setLocal((prev) => ({ ...prev, bass_db: v }))}
          onCommit={(v) => commit("bass_db", v)}
        />
        <TiltSlider
          label="Treble (8 kHz shelf)"
          value={local.treble_db}
          onChange={(v) => setLocal((prev) => ({ ...prev, treble_db: v }))}
          onCommit={(v) => commit("treble_db", v)}
        />
      </div>
    </div>
  );
}

function TiltSlider({
  label,
  value,
  onChange,
  onCommit,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  onCommit: (v: number) => void;
}) {
  return (
    <label className="flex items-center gap-3 text-xs">
      <span className="w-32 flex-shrink-0 text-muted-foreground">{label}</span>
      <input
        type="range"
        min={-12}
        max={12}
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
        className="flex-1 cursor-pointer accent-primary"
      />
      <span className="w-12 flex-shrink-0 text-right tabular-nums">
        {value > 0 ? "+" : ""}
        {value.toFixed(1)} dB
      </span>
    </label>
  );
}

/**
 * Per-device AutoEQ profile mapping — Phase 3 of the scope doc.
 *
 * For each output device the user has used, lets them pick the
 * profile to apply. When a device's mapping changes (or the
 * active device changes), the audio engine resolves the right
 * profile and applies it live.
 *
 * Hidden when the catalog is empty (no profiles bundled) — there
 * would be nothing to map. Otherwise renders even when no
 * devices have been seen yet, with a hint about plugging
 * something in.
 */
interface AutoEqDeviceRow {
  fingerprint: string;
  display_name: string;
  kind: string;
  first_seen: number;
  last_seen: number;
  mapped_profile_id: string | null;
  unmapped?: boolean;
}

/**
 * Per-device profile mapping is a power-user surface — most users
 * have one set of headphones plugged into one DAC. Hide it behind
 * a button + dialog so the Settings page reads as the simple "I
 * have headphones, EQ them" path until the user actually has
 * multiple devices to manage.
 */

function AutoEqDeviceMappingField() {
  const toast = useToast();
  const [data, setData] = useState<{
    devices: AutoEqDeviceRow[];
    current_fingerprint: string;
    fallback: "bypass" | "use_last_profile";
  } | null>(null);
  const [profiles, setProfiles] = useState<
    { id: string; brand: string; model: string }[] | null
  >(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [d, p] = await Promise.all([
          api.player.autoEqDevices(),
          api.player.autoEqList("", 200),
        ]);
        if (cancelled) return;
        setData({
          devices: d.devices,
          current_fingerprint: d.current_fingerprint,
          fallback: d.fallback_when_unmapped,
        });
        setProfiles(
          p.profiles.map((x) => ({ id: x.id, brand: x.brand, model: x.model })),
        );
      } catch {
        /* feature not available — keep section hidden */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const setMapping = async (fingerprint: string, profileId: string | null) => {
    if (!data) return;
    const prev = data;
    setData({
      ...data,
      devices: data.devices.map((d) =>
        d.fingerprint === fingerprint
          ? { ...d, mapped_profile_id: profileId, unmapped: false }
          : d,
      ),
    });
    try {
      await api.player.autoEqSetDeviceMapping(fingerprint, profileId);
    } catch (err) {
      setData(prev);
      toast.show({
        kind: "error",
        title: "Couldn't set device profile",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const forget = async (fingerprint: string) => {
    if (!data) return;
    const prev = data;
    setData({
      ...data,
      devices: data.devices.filter((d) => d.fingerprint !== fingerprint),
    });
    try {
      await api.player.autoEqForgetDevice(fingerprint);
    } catch (err) {
      setData(prev);
      toast.show({
        kind: "error",
        title: "Couldn't forget device",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  if (data === null || profiles === null)
    return (
      <div className="text-xs text-muted-foreground">Loading device list…</div>
    );
  if (profiles.length === 0)
    return (
      <div className="text-xs text-muted-foreground">
        No AutoEQ profiles installed yet. Use the headphone catalog to download
        one, then come back to map devices.
      </div>
    );

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted-foreground">
        {data.devices.length === 0
          ? "Once you've used an output device, it'll show up here so you can map a profile to it."
          : "Tideway swaps EQ profiles automatically when you change output devices."}
      </p>
      <div className="flex flex-col gap-2">
        {data.devices.map((d) => {
          const isActive = d.fingerprint === data.current_fingerprint;
          const value = d.unmapped
            ? "__unmapped__"
            : (d.mapped_profile_id ?? "__none__");
          return (
            <div
              key={d.fingerprint}
              className={cn(
                "flex flex-wrap items-center gap-2 rounded-md border border-input bg-secondary/40 px-3 py-2 text-xs",
                isActive && "border-primary bg-primary/10",
              )}
            >
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="truncate font-semibold">
                  {d.display_name}
                  {isActive && (
                    <span className="ml-2 rounded bg-primary px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-primary-foreground">
                      Active
                    </span>
                  )}
                </span>
                <span className="text-muted-foreground">
                  {d.kind === "unknown" ? "device" : d.kind}
                </span>
              </div>
              <select
                value={value}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "__unmapped__") return; // can't pick unmapped
                  setMapping(d.fingerprint, v === "__none__" ? null : v);
                }}
                className="h-8 rounded-md border border-input bg-background px-2"
              >
                <option value="__unmapped__" disabled>
                  Use fallback
                </option>
                <option value="__none__">No EQ for this device</option>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.brand} {p.model}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => forget(d.fingerprint)}
                className="rounded-md border border-input px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent"
              >
                Forget
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface AirPlayDeviceRow {
  id: string;
  name: string;
  address: string;
  has_raop: boolean;
  paired: boolean;
}

function AirPlaySection() {
  const toast = useToast();
  const [available, setAvailable] = useState<boolean | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const [devices, setDevices] = useState<AirPlayDeviceRow[]>([]);
  const [connectedId, setConnectedId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [pairing, setPairing] = useState<AirPlayDeviceRow | null>(null);
  const [pin, setPin] = useState("");
  const [pinBusy, setPinBusy] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const res = await api.airplay.devices();
      setAvailable(res.available);
      setReason(res.reason ?? null);
      setDevices(res.devices);
      setConnectedId(res.connected_id);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't list AirPlay devices",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const startPair = async (dev: AirPlayDeviceRow) => {
    setPairing(dev);
    setPin("");
    try {
      await api.airplay.pairStart(dev.id);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start pairing",
        description: err instanceof Error ? err.message : String(err),
      });
      setPairing(null);
    }
  };

  const submitPin = async () => {
    if (!pairing || !pin) return;
    setPinBusy(true);
    try {
      await api.airplay.pairPin(pin.trim());
      toast.show({
        kind: "success",
        title: "Paired",
        description: `${pairing.name} is ready to use.`,
      });
      setPairing(null);
      setPin("");
      await refresh();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Pairing failed",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setPinBusy(false);
    }
  };

  const cancelPair = async () => {
    await api.airplay.pairCancel().catch(() => undefined);
    setPairing(null);
    setPin("");
  };

  const connect = async (dev: AirPlayDeviceRow) => {
    setBusyId(dev.id);
    try {
      await api.airplay.connect(dev.id);
      toast.show({
        kind: "success",
        title: "AirPlay connected",
        description: `Sending to ${dev.name}.`,
      });
      await refresh();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't connect",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  };

  const disconnect = async () => {
    try {
      await api.airplay.disconnect();
      await refresh();
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't disconnect",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <Section title="AirPlay" icon={RadioIcon}>
      {available === false && (
        <p className="text-sm text-muted-foreground">
          AirPlay support is unavailable on this install.
          {reason ? ` (${reason})` : null}
        </p>
      )}
      {available !== false && (
        <>
          <div className="flex items-center gap-3">
            <Button
              variant="outline"
              size="sm"
              onClick={refresh}
              disabled={refreshing}
            >
              {refreshing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )}
              Refresh
            </Button>
            {connectedId && (
              <Button variant="ghost" size="sm" onClick={disconnect}>
                <Unlink className="h-3.5 w-3.5" />
                Disconnect
              </Button>
            )}
          </div>
          {devices.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No AirPlay receivers found on this network. Make sure the speaker
              or HomePod is powered on and awake.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {devices.map((dev) => {
                const isConnected = connectedId === dev.id;
                const isBusy = busyId === dev.id;
                return (
                  <li
                    key={dev.id}
                    className="flex items-center gap-3 rounded-md border border-border/40 bg-card/40 p-3"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-semibold">
                        {dev.name}
                      </div>
                      <div className="truncate text-xs text-muted-foreground">
                        {dev.address}
                        {!dev.has_raop &&
                          " — video-only AirPlay, audio streaming unavailable"}
                      </div>
                    </div>
                    {isConnected ? (
                      <span className="flex items-center gap-1 text-xs font-semibold text-primary">
                        <Check className="h-3.5 w-3.5" /> Connected
                      </span>
                    ) : dev.paired ? (
                      <Button
                        size="sm"
                        onClick={() => connect(dev)}
                        disabled={isBusy || !dev.has_raop}
                      >
                        {isBusy ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : null}
                        Connect
                      </Button>
                    ) : (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => startPair(dev)}
                        disabled={!dev.has_raop}
                      >
                        Pair
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          <p className="text-xs text-muted-foreground">
            Audio tees to the connected AirPlay receiver while local output
            keeps playing. Mute your local speakers with the volume slider if
            you don't want both at once. macOS's built-in AirPlay Receiver is
            not supported (Apple's proprietary pairing); HomePods, AirPort
            Express, Apple TVs, and most third-party AirPlay speakers work.
          </p>
        </>
      )}
      {pairing && (
        <div className="mt-2 rounded-md border border-primary/40 bg-primary/5 p-4">
          <div className="mb-1 text-sm font-semibold">
            Pairing with {pairing.name}
          </div>
          <p className="mb-3 text-xs text-muted-foreground">
            A 4-digit PIN should appear on the receiver now. If the receiver is
            a HomePod, check the paired iPhone or iPad; some models display the
            PIN there.
          </p>
          <div className="flex items-center gap-2">
            <Input
              value={pin}
              onChange={(e) => setPin(e.target.value)}
              placeholder="0000"
              inputMode="numeric"
              maxLength={8}
              className="max-w-[8rem]"
              autoFocus
            />
            <Button onClick={submitPin} disabled={pinBusy || !pin}>
              {pinBusy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : null}
              Submit
            </Button>
            <Button variant="ghost" onClick={cancelPair}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </Section>
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
  const [busy, setBusy] = useState<
    null | "save" | "connect" | "complete" | "disconnect"
  >(null);

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
      const s = await api.lastfm.setCredentials(
        apiKey.trim(),
        apiSecret.trim(),
      );
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
              Scrobbling as{" "}
              <span className="text-foreground">{status.username}</span>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={disconnect}
            disabled={busy !== null}
          >
            {busy === "disconnect" && (
              <Loader2 className="h-4 w-4 animate-spin" />
            )}
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
              {busy === "connect" && (
                <Loader2 className="h-4 w-4 animate-spin" />
              )}
              {pendingToken ? "Open Last.fm again" : "Connect"}
            </Button>
            {pendingToken && (
              <Button
                onClick={completeConnect}
                disabled={busy !== null}
                size="sm"
              >
                {busy === "complete" && (
                  <Loader2 className="h-4 w-4 animate-spin" />
                )}
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
              Browser should have opened to Last.fm. Approve the app, then click
              "I've approved".
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
            if you don't have one — any application name works, callback URL can
            be blank.
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

/**
 * OS-level autostart toggle. State lives in the OS (launchd plist on
 * macOS, registry value on Windows, XDG desktop file on Linux) — not
 * in settings.json — because it has to survive the app being replaced
 * / reinstalled / moved. Component fetches its own status and writes
 * directly; no coupling to the Settings dataclass.
 */
function AutostartSection() {
  const toast = useToast();
  const [status, setStatus] = useState<{
    available: boolean;
    enabled: boolean;
    path: string | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.autostart
      .status()
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch(() => {
        if (!cancelled)
          setStatus({ available: false, enabled: false, path: null });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!status) return null;

  const flip = async (v: boolean) => {
    if (!status.available) return;
    setBusy(true);
    try {
      const next = await api.autostart.set(v);
      setStatus(next);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't change auto-start",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      title="Launch on login"
      icon={Power}
      description={
        status.available
          ? "Start Tideway automatically when you log in. Handled by the OS — no background service runs while this is off."
          : "Auto-start only works in the packaged app. Run from the built .app / .exe to enable this."
      }
    >
      <label className="flex items-center gap-3 text-sm">
        <input
          type="checkbox"
          checked={status.enabled}
          onChange={(e) => flip(e.target.checked)}
          disabled={!status.available || busy}
          className="h-4 w-4 accent-primary"
        />
        Start Tideway when I log in
        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
      </label>
      {status.available && status.path && (
        <div className="text-xs text-muted-foreground">
          Will launch:{" "}
          <code className="rounded bg-secondary px-1.5 py-0.5">
            {status.path}
          </code>
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

// Public repo. Used both as the source-code link and the destination
// for "Report an issue" — the issue tracker lives there. Keeping it
// as a single const here means a fork only has to retag this one
// place to point links at their own infrastructure.
const TIDEWAY_REPO_URL = "https://github.com/J-M-PUNK/tideway";

/**
 * About tab — version, update status, and outbound links to the
 * project repo and issue tracker. Reuses the same /api/version and
 * /api/update-check endpoints that drive the in-app update banner,
 * so what shows here is consistent with the banner's logic. The
 * panel does its own fetches because Settings doesn't otherwise
 * need version data — there's no shared store to subscribe to.
 */
function AboutSection() {
  const toast = useToast();
  const [version, setVersion] = useState<string | null>(null);
  const [update, setUpdate] = useState<{
    available: boolean;
    latest: string | null;
    url: string | null;
  } | null>(null);
  const [savingReport, setSavingReport] = useState(false);

  const saveReport = async () => {
    if (savingReport) return;
    setSavingReport(true);
    try {
      const res = await api.saveActivityReport();
      // Quote the full path back to the user — they need to be able
      // to attach this file to a bug report. Long paths wrap in the
      // toast description, which is fine.
      toast.show({
        kind: "success",
        title: "Activity report saved",
        description: res.path,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't save activity report",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSavingReport(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    api
      .version()
      .then((r) => {
        if (!cancelled) setVersion(r.version);
      })
      .catch(() => {
        if (!cancelled) setVersion(null);
      });
    api
      .updateCheck()
      .then((r) => {
        if (!cancelled)
          setUpdate({
            available: r.available,
            latest: r.latest,
            url: r.url,
          });
      })
      .catch(() => {
        // Update probe failed (offline, GitHub rate limit, repo
        // private). Show "version only" rather than a misleading
        // "up to date".
        if (!cancelled) setUpdate(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Section title="About" icon={Info}>
      <div className="flex flex-col gap-1">
        <div className="text-sm text-muted-foreground">Version</div>
        <div className="text-2xl font-bold tracking-tight">
          {version ? `Tideway ${version}` : "Tideway"}
        </div>
        {update && update.available && update.latest && (
          <div className="mt-1 flex items-center gap-2 text-sm">
            <span className="rounded bg-primary/15 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-primary">
              Update available
            </span>
            <span className="text-muted-foreground">
              {update.latest} is out
            </span>
            {update.url && (
              <a
                href={update.url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 text-primary hover:underline"
              >
                Release notes <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        )}
        {update && !update.available && version && (
          <div className="mt-1 text-sm text-muted-foreground">
            <Check className="mr-1 inline h-3.5 w-3.5 text-sky-500" />
            You're on the latest release.
          </div>
        )}
      </div>

      <div className="mt-2 flex flex-col gap-2">
        <a
          href={TIDEWAY_REPO_URL}
          target="_blank"
          rel="noreferrer noopener"
          className="group flex items-center gap-3 rounded-md border border-border/50 bg-card/60 p-3 text-sm transition-colors hover:bg-accent/40"
        >
          <Code2 className="h-4 w-4 text-primary" />
          <div className="flex-1">
            <div className="font-semibold">GitHub repo</div>
            <div className="text-xs text-muted-foreground">
              Source code, releases, license.
            </div>
          </div>
          <ExternalLink className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </a>
        <a
          href={`${TIDEWAY_REPO_URL}/issues/new`}
          target="_blank"
          rel="noreferrer noopener"
          className="group flex items-center gap-3 rounded-md border border-border/50 bg-card/60 p-3 text-sm transition-colors hover:bg-accent/40"
        >
          <Bug className="h-4 w-4 text-primary" />
          <div className="flex-1">
            <div className="font-semibold">Report an issue</div>
            <div className="text-xs text-muted-foreground">
              Bug, regression, or feature request.
            </div>
          </div>
          <ExternalLink className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </a>
        <button
          type="button"
          onClick={saveReport}
          disabled={savingReport}
          className="group flex items-center gap-3 rounded-md border border-border/50 bg-card/60 p-3 text-left text-sm transition-colors hover:bg-accent/40 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {savingReport ? (
            <Loader2 className="h-4 w-4 animate-spin text-primary" />
          ) : (
            <FileDown className="h-4 w-4 text-primary" />
          )}
          <div className="flex-1">
            <div className="font-semibold">Save activity report</div>
            <div className="text-xs text-muted-foreground">
              Writes a JSON snapshot to your Downloads folder. Useful for
              attaching to a bug report. Settings credentials are stripped.
            </div>
          </div>
        </button>
      </div>
    </Section>
  );
}
