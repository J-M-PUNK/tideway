# Headphone-aware AutoEQ — implementation scope

Tideway already has a 10-band manual EQ (`app/audio/eq.py`). That's
fine for tinkerers, but the audiophile experience users actually
want from a player like this is **automatic per-headphone correction**:
plug in your HD 600s, the EQ applies the right curve; switch to
your IEM, the curve switches; flat for line-out.

This doc scopes that work. It builds on top of the existing
manual-EQ machinery rather than replacing it; the two modes
coexist.

## Why this matters

Roon and Audirvana cost money and are widely used because of their
DSP stack — parametric EQ, headphone correction, room correction.
The official Tidal client has none of it. AutoEQ + per-device
mapping is the cheapest, highest-leverage step toward "Tideway is
the player audiophiles use to play Tidal" rather than "Tideway is
a slightly nicer Tidal client."

[AutoEQ](https://github.com/jaakkopasanen/AutoEq) is an open
catalogue of measured frequency responses for ~5,000 headphones,
each shipped with a generated parametric EQ that corrects the
measured curve toward Harman / B&K / similar target curves.
Repeatedly cited in audiophile forums as the thing that makes
PEACE / EqualizerAPO / Auris worth installing. Free, MIT-licensed
data, well-maintained, and stable enough that vendoring a snapshot
into Tideway is straightforward.

## Architecture

The audio path becomes (new stages marked **bold**):

```
PCM source
   ↓
[Master preamp]                  ← profile.preamp + user.preamp_offset
   ↓
[Profile biquad cascade]         ← AutoEQ bands (PK/LSC/HSC), per sample rate
   ↓
**[Tilt biquad pair]**           ← user's bass + treble shelves
   ↓
**[Dither]**                     ← TPDF, only when downsizing bit depth
   ↓
Output device
```

Cascade is a single `(N, 6)` SOS matrix fed to `scipy.signal.sosfilt`
with state preserved across coefficient swaps — the same shape
`app/audio/eq.py` already builds for the 10-band manual EQ.

The EQ has three modes that coexist:

- **`profile`** — uses the cascade above. Active profile chosen
  by output-device mapping.
- **`manual`** — the existing 10-band EQ, untouched. For users
  who want to tinker.
- **`off`** — bypass the EQ stage entirely.

Settings field `eq.mode: "profile" | "manual" | "off"` selects
which path runs. Audio thread reads it once per callback. Mode
switches don't destroy the other mode's state — switching from
profile to manual and back keeps both configurations intact.

## What we're building (user-facing)

1. User opens Settings → EQ → Headphone profile.
2. Searchable picker (5,000 entries, fuzzy match) lets them
   choose their headphones.
3. The selected profile applies immediately. EQ ON / EQ BYPASS
   button next to the now-playing bar lets them A/B the
   correction.
4. Three taste sliders: preamp offset, bass tilt, treble tilt.
   Live update as they drag.
5. Optional: a frequency-response graph showing raw curve,
   target, and post-EQ predicted response.
6. Per-device memory: the chosen profile is remembered against
   the active output device. Switching DACs / unplugging
   headphones swaps profiles automatically.

## Phases

Effort numbers are honest "focused work" estimates, not optimistic
ones. Each phase ends with a usable artifact even if later phases
slip.

### Phase 1 — shelf filter support (~1 afternoon)

Has to come first because every later phase assumes the cascade
can do shelves.

- Add `low_shelf` and `high_shelf` filter types to the biquad
  coefficient calculator alongside the existing `peaking`
  ([eq.py:145](app/audio/eq.py:145)). Standard RBJ Audio EQ Cookbook
  formulas. Q for shelves uses RBJ's "S" slope convention
  rather than bandwidth Q — document this and unit-test it.
- The cascade structure (`sosfilt` + `zi` state) doesn't change.
  Same `(N, 6)` SOS array shape, different per-row coefficients.
- New unit test: load a known AutoEQ profile (e.g. HD 400 Pro),
  run a frequency sweep through the cascade, compare the
  magnitude response against the AutoEQ-published preview within
  ±0.1 dB tolerance.

**Output:** the existing 10-band EQ now supports shelves
internally. Manual UI stays peaking-only; the profile path uses
shelves freely.

### Phase 2 — profile loader and picker (~1-2 evenings)

The first user-visible feature. After this phase the user can
manually pick a headphone and hear the correction.

**Bundled data:**

- Vendor a snapshot of `jaakkopasanen/AutoEq`'s `results/`
  directory into `app/audio/autoeq/data/results/`. Few MB.
- Vendor `measurements/` too — needed in Phase 6 for the FR
  graph. Larger but still <50 MB.
- Update `Tideway-mac.spec` and `Tideway-win.spec` to include
  the data directory in `datas`.

**New backend module `app/audio/autoeq/`:**

- `profiles.py` — parser for the `*ParametricEQ.txt` files in
  AutoEQ. Builds `AutoEqProfile` dataclasses. Indexes them by
  id, brand, model.
- `index.py` — in-memory search index, `rapidfuzz` for fuzzy
  matching. Loaded once at startup; 5,000 entries is small.
- `apply.py` — converts a profile + user offsets into an SOS
  coefficient array at a given sample rate.

**FastAPI endpoints:**

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/eq/profiles?q=<query>&limit=50` | Searchable list |
| `GET` | `/api/eq/profiles/{id}` | Full profile detail |
| `GET` | `/api/eq/state` | Current mode, active profile, tilts, device |
| `POST` | `/api/eq/load-profile` | `{profile_id}` |
| `POST` | `/api/eq/mode` | `{mode: "profile"\|"manual"\|"off"}` |

**Frontend:**

- New section in the existing EQ panel: "Headphone profile."
- Mode toggle at the top: Off / Profile / Manual.
- Profile picker: shadcn Command combobox, brand → model.
  Search is client-side after initial fetch.
- "Selected profile" card showing brand / model / source
  ("oratory1990" etc.) / target curve / band count / recommended
  preamp.
- A/B bypass button — separate from the mode toggle; bypasses
  the EQ stage for momentary comparison.

**Validation:** pick HD 400 Pro from the picker, hear the
difference, A/B compare.

### Phase 3 — per-device profile mapping (~1 weekend)

The headline differentiator. Plug headphones in, the right
correction applies automatically.

**Persisted seen-devices list** in `seen_devices.json` under
`user_data_dir()`:

```json
[
  { "fingerprint": "...", "display_name": "...",
    "kind": "bt|usb|builtin",
    "first_seen": 1735000000, "last_seen": 1735999999 }
]
```

Fingerprint = device name as `sounddevice` reports it. Stable
enough; both BT and USB names persist across reconnects on
Windows / macOS / Linux. On every device enumeration, upsert.

**Settings schema additions** (extends existing `EqSettings`):

```yaml
eq:
  mode: "profile" | "manual" | "off"
  device_mappings: { fingerprint: profile_id | null }
  user_tilt:
    preamp_offset_db: float    # -12 .. +12
    bass_db: float              # -12 .. +12
    treble_db: float            # -12 .. +12
  fallback_when_unmapped: "bypass" | "use_last_profile"
```

**Backend logic:**

- A profile resolver runs whenever the active output device
  changes. Looks up `device_mappings[active_fingerprint]`,
  loads the profile, recomputes coefficients for the active
  sample rate, hands to the audio thread.
- The audio thread already supports coefficient swaps (it does
  today for manual EQ band changes); the profile path uses the
  same hook.
- New endpoint `POST /api/eq/device-mappings` —
  `{fingerprint, profile_id | null}`.
- Existing SSE stream gets a new event type
  `eq_state_changed` carrying the active profile, mode, and
  device.

**Frontend:**

- New Settings page section: "Output device profiles."
- Lists every seen device. Each row: device name, kind
  (BT / USB / built-in), profile dropdown, last-used timestamp.
- Currently active device highlighted / pinned to top.
- "Forget device" affordance for cleanup.

**One-time UX touch:** the first time a user maps a profile to
a Bluetooth device, show a one-time toast:

> Bluetooth audio is processed by the OS codec and the
> headphone's onboard DSP, so the EQ won't be as accurate as
> wired listening. Still useful, just not perfect.

### Phase 4 — A/B toggle and signal path display (~1 weekend)

Polish that turns "feature exists" into "feature feels good."

**A/B toggle:**

- Big visible button in the player UI, not buried in settings.
- Single keystroke shortcut (suggest `E`; unbound and mnemonic).
  Window-scoped, not global media-key — the global media keys
  are for transport control and shouldn't double up.
- Implements as a "bypass EQ stage" flag the audio thread
  reads. Instant, no fade. The brief click is fine and is
  actually useful — it's the audible boundary that proves the
  EQ is doing something.
- Visual state: button shows "EQ ON" / "EQ BYPASS" with color
  change.

**Signal path display** on the now-playing screen (expanded
view, not the mini-player). One-line read-only summary like:

> FLAC 24/96 → −6.5 dB preamp → 8-band EQ (HD 400 Pro / oratory1990) → +2 dB bass tilt → WASAPI exclusive → Scarlett Solo USB

Pulls from existing player state + the new EQ state SSE.
Hover/tap a segment for detail (e.g. hover the EQ segment to
see all band frequencies and gains).

This is the feature where Tideway starts feeling like Roon.
Cheap to ship, disproportionately impressive.

### Phase 5 — user tilt layer (~1 weekend)

The "I love the profile but want more bass" feature.

**Backend:**

- Append two biquads to the cascade after the profile bands:
  - **Bass:** low shelf at 80 Hz, fixed Q (~0.7), gain from
    `user_tilt.bass_db`.
  - **Treble:** high shelf at 8 kHz, fixed Q (~0.7), gain from
    `user_tilt.treble_db`.
- Master preamp offset `user_tilt.preamp_offset_db` adds to
  the profile's recommended preamp.
- Tilts are **user-global**, not per-device. Reasoning: it's a
  taste preference that travels with the listener, not a
  per-headphone correction.
- Endpoints: `POST /api/eq/tilt` for live updates.

**Frontend:**

- Three sliders below the active profile card:
  - Preamp offset: −12 to +12 dB
  - Bass: −12 to +12 dB
  - Treble: −12 to +12 dB
- Live update as you drag; debounce coefficient recompute to
  ~50 ms.
- "Reset tilt" button.

**Headroom warning:** when bass tilt is positive and the
profile preamp is small, total gain across the cascade can
exceed 0 dB at some frequency, causing clipping. The preamp
offset slider should warn when this happens. Computable from
the cascade's frequency response.

### Phase 6 — frequency response graph (~1 weekend, mostly frontend)

The Roon-tier polish.

**Backend:**

- New endpoint `GET /api/eq/response?points=512` returning
  three arrays:
  - Frequency points (log-spaced 20 Hz to 20 kHz)
  - Headphone raw measured response (from bundled `measurements/`)
  - Target curve (from profile metadata)
  - Post-EQ predicted response (raw + cascade applied at the
    current sample rate)
- Computed on demand using `scipy.signal.sosfreqz`. Cheap.

**Frontend:**

- Graph component below the profile card. Three overlaid
  lines: raw (gray), target (dashed), post-EQ (solid).
- Log frequency axis, dB amplitude axis. Legend.
- Use whatever charting lib is already pulled in for SSE-driven
  UI (Recharts likely).
- Updates live as user drags tilt sliders.

This is where the project starts feeling like it could charge
money. Also great for screenshots.

### Phase 7 — profile freshness (later, ~1 weekend)

Not blocking initial release. Worth scheduling for v1.x.

- "Check for AutoEQ updates" button in Settings.
- Pulls a manifest from a known URL (self-hosted in this repo's
  `gh-pages`, or a JSON committed to a known tag of the AutoEQ
  repo if one becomes available).
- Diffs against the bundled snapshot. Downloads new/changed
  profiles into app data, which takes precedence over the
  bundled copies.
- Surfaces count of new profiles since last check.

## Cross-cutting concerns

**Dither.** Bit-perfect output + AutoEQ-induced gain changes
means dither matters. If we're not already TPDF-dithering when
going from internal float to output int24/int16, add it as part
of Phase 1. ~10 lines of numpy.

**Sample rate transitions.** Coefficients are sample-rate-
dependent, profile data is not. The manual EQ already recomputes
on rate change ([eq.py:67-105](app/audio/eq.py:67)); the profile
path uses the same hook. Test specifically with 44.1 → 96 → 192
transitions while a profile is active.

**Coefficient hot-swap clicks.** Swapping the cascade mid-stream
causes a transient if filter state isn't preserved. `scipy`'s
`sosfilt_zi` gives steady-state initial conditions; for hot
swap we want to **preserve** the existing `zi` across the swap,
not reinitialise. There'll be a brief response transient as the
new filter settles — inaudible for typical changes, may click
on dramatic profile switches. Fine for v1; revisit if reported.

**Two EQ states coexisting.** The user can have manual-mode
bands tweaked AND a profile-mode profile mapped to a device.
Mode switching just selects which path is live; both states
persist independently.

**Recovery path.** New users will load a profile, dislike it,
and panic. The off-switch must be obvious: mode toggle at the
top of the EQ panel with a clear "Off" option, plus the A/B
button for momentary comparison. That's enough.

**Existing manual EQ unchanged.** Phase 1 adds shelf support
internally but the manual UI stays peaking-only. No user-facing
behavior change for users on manual mode.

## Suggested rollout

Ship **Phases 1-3 as one release** ("Headphone profiles in
Tideway") — they form a complete feature. Picking a headphone,
hearing correction, having it remember per-device. That's a
real, marketable product.

Ship **Phases 4-6 as a follow-up** ("EQ workshop" or similar
polish push) — A/B, signal path display, FR graph. These are
the features that turn a working tool into something people
post screenshots of.

Phase 7 anytime after.

## Risks

**License of bundled data.** AutoEQ ships under MIT; vendoring
into Tideway's MIT codebase is fine. Verify before adding to
the bundled package.

**Bundle size.** Vendoring `results/` + `measurements/` adds
~50-100 MB to the app. The current installer is much smaller
than that. Either accept the size growth or fetch on first run
(adds startup latency + offline-first concern). Recommend
accepting the growth — predictable startup is worth the disk.

**AutoEQ project maintenance.** The project is well-maintained
today (last commit recent, regular updates). If it ever stalls,
profiles for new headphones won't appear. Mitigation: phase 7
update path; if AutoEQ stalls, users still have a working
snapshot.

**False precision.** The measured FR curves AutoEQ uses are
specific to a particular measurement rig. Real headphones vary
unit-to-unit; user's specific pair may sound subtly different
from the average. The tilt sliders give users a way to nudge
toward their preference, which is the right escape valve.

**No replacement for user judgment.** AutoEQ targets are
defaults (Harman, etc.). Some users prefer different curves —
"more bass," "less treble" etc. The tilt layer covers this for
the casual case; users with very specific preferences can fall
back to manual mode.

## Open questions

These don't block scoping but are worth explicitly noting:

1. Should the per-device mapping persist across machines (e.g.,
   the same DAC plugged into laptop vs desktop)? If yes, sync
   strategy needed. Probably no for v1 — ship local-only,
   reassess if users ask.
2. Should we surface the AutoEQ source attribution in the UI?
   ("This profile by oratory1990 from autoeq.app".) Yes, in
   the profile detail card. Important for credit and credibility.
3. Should the "Headphones" preset in the manual EQ
   ([eq.py:201](app/audio/eq.py:201)) be deprecated once profile
   mode ships? Probably leave it — it's a no-data fallback for
   users who don't know their headphone model.
4. Bluetooth-aware behavior: should we automatically switch to
   `mode="off"` on BT output? Probably no — accuracy is reduced
   but not zero, and the toast warning gives the user the
   information to decide.

## Effort budget

| Phase | Effort | Cumulative |
|---|---|---|
| 1 — shelf filters | ~1 afternoon | 0.5 day |
| 2 — loader + picker | ~1-2 evenings | 1.5 days |
| 3 — per-device mapping | ~1 weekend | 4 days |
| 4 — A/B + signal path | ~1 weekend | 6 days |
| 5 — tilt layer | ~1 weekend | 8 days |
| 6 — FR graph | ~1 weekend | 10 days |
| 7 — freshness | ~1 weekend | 12 days |

Phases 1-3 (the marketable core) are **~4 days** of focused
work. Phases 4-6 (the polish push) add ~6 days. Phase 7 is
optional, ~2 days.

## What we're explicitly NOT building

- **Room correction** (REW / Dirac integration). Different
  problem domain, requires user measurement workflow we don't
  have. Out of scope, possibly forever.
- **VST/VST3 plugin hosting.** Different scope; users wanting
  more flexibility have manual EQ + tilt sliders.
- **Custom target curves.** Use AutoEQ's published targets.
  Letting users define their own targets is feature creep on
  top of an already-substantial scope.
- **Crossfeed / spatializer.** Worth doing later as a separate
  feature, not part of AutoEQ.
- **Real-time DSP measurement of the user's setup.** Would
  require a calibrated mic + measurement workflow. Out of
  scope.

## References

- [AutoEQ project](https://github.com/jaakkopasanen/AutoEq)
  (data + generation pipeline)
- [autoeq.app](https://autoeq.app/) (user-facing search UI we
  partially mirror)
- RBJ Audio EQ Cookbook (biquad coefficient formulas — used
  by the existing `app/audio/eq.py` peaking implementation)
- Existing manual EQ: [app/audio/eq.py](app/audio/eq.py)
- Strategic positioning context: see "Tideway audiophile
  feature research" (kept in `private/research/` while in
  draft form).
