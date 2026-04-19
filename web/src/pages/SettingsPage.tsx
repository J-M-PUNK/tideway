import { useEffect, useState } from "react";
import { LogOut, Save, Settings as SettingsIcon } from "lucide-react";
import { api } from "@/api/client";
import type { QualityOption, Settings } from "@/api/types";
import { Button } from "@/components/ui/button";
import { publishDefaultQuality } from "@/components/DownloadButton";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/toast";
import { useUiPreferences } from "@/hooks/useUiPreferences";

export function SettingsPage({ onLogout }: { onLogout: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [qualities, setQualities] = useState<QualityOption[]>([]);
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const ui = useUiPreferences();

  const [loadError, setLoadError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, qs] = await Promise.all([api.settings.get(), api.qualities()]);
        if (cancelled) return;
        setSettings(s);
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

  if (loadError)
    return (
      <div className="text-sm text-destructive">Couldn't load settings: {loadError.message}</div>
    );
  if (!settings) return <div className="text-sm text-muted-foreground">Loading…</div>;

  const patch = (p: Partial<Settings>) => setSettings({ ...settings, ...p });

  const save = async () => {
    setSaving(true);
    try {
      const s = await api.settings.put(settings);
      setSettings(s);
      // Notify open DownloadButtons so their "Use default" checkmark
      // moves to the new quality immediately — without this they show
      // the old default until a hard reload.
      publishDefaultQuality(s.quality);
      toast.show({ kind: "success", title: "Settings saved" });
    } catch (err) {
      // Re-pull server truth so the form doesn't stay on an invalid
      // output_dir the user just typed — otherwise repeated Saves
      // retry the same rejected payload.
      try {
        const s = await api.settings.get();
        setSettings(s);
      } catch {
        /* best-effort */
      }
      toast.show({
        kind: "error",
        title: "Save failed",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSaving(false);
    }
  };

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
        <Toggle
          checked={ui.offlineOnly}
          onChange={(v) => ui.set({ offlineOnly: v })}
          label="Show only downloaded tracks in lists"
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
        <Button onClick={save} disabled={saving}>
          <Save className="h-4 w-4" /> {saving ? "Saving…" : "Save"}
        </Button>
        <Button variant="outline" onClick={onLogout}>
          <LogOut className="h-4 w-4" /> Log out
        </Button>
      </div>
    </div>
  );
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
