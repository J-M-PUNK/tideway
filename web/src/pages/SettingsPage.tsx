import { useEffect, useRef, useState } from "react";
import { Check, Loader2, LogOut, Moon, Settings as SettingsIcon, Sun } from "lucide-react";
import { api } from "@/api/client";
import type { QualityOption, Settings } from "@/api/types";
import { Button } from "@/components/ui/button";
import { publishDefaultQuality } from "@/components/DownloadButton";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/toast";
import { useOfflineMode } from "@/hooks/useOfflineMode";
import { useUiPreferences, type ThemeMode } from "@/hooks/useUiPreferences";
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

      <Section title="Downloads" description="Where and how your music is saved.">
        <Field label="Output folder">
          <Input
            value={settings.output_dir}
            onChange={(e) => patch({ output_dir: e.target.value })}
            placeholder="/path/to/music"
          />
        </Field>

        <Field
          label="Default quality"
          hint="Used for any download that doesn't override it. Your account must support the selected quality."
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
        title="Display"
        description="Local-only preferences — stored on this device, not synced to Tidal."
      >
        <Field label="Theme">
          <ThemePicker value={ui.theme} onChange={(t) => ui.set({ theme: t })} />
        </Field>
        <Toggle
          checked={ui.offlineOnly}
          onChange={(v) => ui.set({ offlineOnly: v })}
          label="Show only downloaded tracks in lists"
        />
      </Section>

      <Section
        title="Offline mode"
        description="Browse and play music already on this device without signing in to Tidal. Search, explore, and anything that needs a live session are hidden while offline mode is on."
      >
        <Toggle
          checked={settings.offline_mode}
          onChange={(v) => patch({ offline_mode: v })}
          label="Work offline"
        />
      </Section>

      <Section
        title="Notifications"
        description="Show a desktop notification when a batch of downloads finishes. Your browser will prompt for permission the first time a download completes after this is on."
      >
        <Toggle
          checked={settings.notify_on_complete}
          onChange={(v) => patch({ notify_on_complete: v })}
          label="Notify me when downloads finish"
        />
      </Section>

      <Section title="Keyboard shortcuts" description="Speed up your navigation.">
        <ShortcutRow keys={["⌘", "K"]} label="Focus search" />
        <ShortcutRow keys={["Space"]} label="Play / pause preview" />
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
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-10 flex flex-col gap-5 rounded-lg border border-border/50 bg-card/40 p-6">
      <div>
        <h2 className="text-lg font-semibold">{title}</h2>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
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
