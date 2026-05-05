# Parametric EQ and AutoEQ profiles

Tideway's audio pipeline includes a parametric EQ stage that runs
between the decoder output and the OS audio API. This doc covers
the three EQ modes, how to import an AutoEQ profile, and the
options that stack on top of a profile.

## The three modes

The EQ mode lives in **Settings → EQ**. There are three options:

- **Off** — the EQ stage is bypassed. The PCM samples reach your
  DAC unmodified. This is the default for new installs and the
  right choice if you're chasing bit-perfect output or if your
  DAC / headphone amp already has its own correction.
- **Manual** — a 10-band parametric EQ with an integer dB preamp
  and a small library of named presets (Bass Boost, Classical,
  Vocal Boost, etc.). Each preset shows a curve preview in the
  picker so you can recognise the shape without applying it.
  Bands at the extremes use shelf filters; the middle bands are
  peaking filters.
- **Profile** — load an AutoEQ correction file targeting a
  specific pair of headphones. See the next section for how to
  get one.

Switching modes is live: there is no audio dropout, no need to
restart playback. The EQ stage rebuilds its filter cascade in the
audio callback's setup phase on the next track or seek.

## AutoEQ profiles

[AutoEQ](https://github.com/jaakkopasanen/AutoEq) is a community
project that publishes per-headphone parametric EQ corrections
based on measured frequency response. The corrections target one
of three reference curves: a flat / neutral target, the Harman
target (a slight bass and treble lift that most listeners
prefer), and the B&K target (an older, slightly gentler curve).

Tideway does not browse AutoEQ's catalog and does not download
profiles for you. Profiles are user-imported. The expected flow:

1. Find your headphones on AutoEQ's site or its GitHub repo.
2. Download a `ParametricEQ.txt` file from the headphone's folder.
3. In Tideway, **Settings → EQ → Profile → Import profile**.
4. Pick the file. Tideway parses it, validates each filter line,
   and the profile becomes the active EQ.

The format is simple, regular, and small:

    Preamp: -6.5 dB
    Filter 1: ON LSC Fc 105 Hz Gain 6.0 dB Q 0.7
    Filter 2: ON PK Fc 200 Hz Gain -3.0 dB Q 1.41
    Filter 3: ON HSC Fc 8000 Hz Gain -2.0 dB Q 0.7
    ...

`PK` is a peaking filter, `LSC` is a low shelf, `HSC` is a high
shelf. Tideway also accepts files exported from
[Equalizer APO](https://sourceforge.net/projects/equalizerapo/),
whose layout is similar enough that the same parser handles both
with a small fallback path. If a file is malformed, the import
surfaces the offending line rather than silently dropping it.

## A / B bypass

Once a profile is loaded, the **A/B** toggle next to the mode
picker temporarily routes audio through the same path with the EQ
stage muted. The intent is comparison: hold the toggle while
listening to a passage, release it, listen to the same passage
unprocessed. Useful for deciding whether the correction is doing
what you wanted, and for catching cases where the profile is too
aggressive in a region your music doesn't really exercise.

## User tilt

Stacked **after** the profile bands, Tideway optionally applies
three sliders the user controls directly:

- **Bass** — a low shelf at 80 Hz.
- **Treble** — a high shelf at 8 kHz.
- **Preamp offset** — an integer dB nudge to the master gain on
  top of whatever the profile already sets.

Tilt is taste-layer adjustment, not correction. The profile
correction lands on a clean signal first; tilt sits on top so you
can warm up or brighten the result without modifying the profile
file. Setting all three to zero collapses the tilt stage out
entirely (no extra biquad), so there's no fixed cost when you're
not using it.

## Frequency-response graph

The Settings page shows a graph of the cumulative EQ response.
Profile bands draw in one colour, tilt shelves draw in a second,
and the sum draws on top. The graph re-renders live as you
change tilt sliders, so you can see exactly what the cumulative
correction looks like across the audible range.

## Per-device profile mapping

If you swap between a few different pairs of headphones, you
probably want each one's profile to apply automatically. The
**Per-device mapping** section under Settings remembers which
profile to use for each output device the OS exposes. When you
switch outputs (USB DAC, Bluetooth, builtin speakers), Tideway
checks the mapping and loads the corresponding profile if one is
configured. Devices without a mapping fall back to whatever
profile was last active, or to no profile if you've never loaded
one.

## File locations

Imported profiles are copied into Tideway's app-data directory so
they're not lost if you delete the source file. On macOS that's
`~/Library/Application Support/Tideway/autoeq/profiles/`. The
per-device mapping is stored in the same settings JSON as
everything else.

## Limitations

- **Tideway doesn't measure your headphones.** Bring your own
  profile.
- **Filter count is bounded.** Profiles with more than 32 bands
  are rejected on import; nobody needs that many for a pair of
  headphones, and the cap protects the audio callback's deadline.
- **Profile files are PEQ-only.** AutoEQ also publishes a
  graphic-EQ format (per-octave bands) and a convolution / FIR
  format (a `.wav` impulse response). Tideway only handles the
  parametric format, which is the one most users want and the
  only one that fits the existing biquad pipeline.
