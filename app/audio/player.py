"""PCMPlayer — PyAV + sounddevice audio engine.

Decodes DASH / local audio with PyAV and drives a sounddevice
OutputStream at the track's native sample rate + native format.
No software resampling or format conversion anywhere in the
playback path; samples hit CoreAudio / WASAPI / ALSA bit-identical
to what's in the source.

Gapless transitions: preloaded next-track PCM is spliced into the
live OutputStream at the sample boundary (same-rate) or triggers a
~50ms stream reopen (cross-rate). Both paths sit behind `preload()`
and fire automatically on natural end-of-track.

Threading model:
  - Main thread (HTTP handlers): calls load/play/pause/resume/seek/
    stop/set_volume/set_muted. Mutates state through a lock; never
    blocks on the audio pipeline.
  - Decoder thread: pulls PCM chunks from `Decoder`, pushes into
    `_pcm_queue`. Exits at EOF or when `_stop_flag` is set. Seek
    is applied in-flight: `seek()` calls `Decoder.request_seek()`
    + drains the queue, and the decoder picks up the seek on its
    next iteration.
  - Audio callback (sounddevice realtime thread): drains the queue,
    fills output buffers. Honours the `_paused` flag (outputs
    silence, doesn't drain) and applies volume/mute.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import requests
import sounddevice as sd  # type: ignore
import tidalapi

from app.audio.decoder import Decoder
from app.audio.crossfade import mix_crossfade_block
from app.audio.crossfeed import Crossfeed
from app.audio.eq import (
    Equalizer,
    ParametricBand,
    build_parametric_sos,
    parametric_band_from_dict,
    parametric_preset,
)
from app.audio.manifest_cache import ManifestCache
from app.audio.replaygain import (
    ReplayGain,
    ReplayGainMode,
    ReplayGainTags,
    VALID_MODES as REPLAYGAIN_VALID_MODES,
    compute_gain_db as compute_replaygain_db,
)
from app.audio.output_devices import list_output_devices, resolve_output_device
from app.audio.segment_reader import SegmentReader

# Optional audio output taps. Imported at module load (not lazily
# from inside the audio callback) because Python's `import` statement
# acquires the import lock to look up an already-cached module, and
# during a concurrent cold-load elsewhere in the process — Spotify
# enrichment first-loading spotapi when the user opens an artist page
# is the canonical case — that lock can be held for hundreds of ms.
# A lazy `import` inside the callback would block on it for the same
# duration and starve the realtime audio thread. The user hears that
# as stutter and clipping during cold artist-page navigation. With
# the imports here, the callback does plain attribute reads on
# already-loaded modules; the import lock is never touched on the
# realtime path. If the optional dep is missing on this machine
# (pyatv not installed, etc.) the reference stays None and the
# corresponding tap branch is skipped.
try:
    from app.audio import cast as _cast_mod
except Exception:  # noqa: BLE001
    _cast_mod = None  # type: ignore[assignment]
try:
    from app.audio.upnp import upnp_manager as _upnp_manager
except Exception:  # noqa: BLE001
    _upnp_manager = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Persistent audio-event log. A Finder-launched .app discards
# stdout/stderr, so the [audio]/[pcm] prints below never survive a
# real incident — an audio glitch was undebuggable from logs. This
# rotating file in the app data dir does survive, mirroring how
# window_chrome keeps its own. Realtime-safe: the callback emitters
# are rate-limited to ~1/sec and the swap line fires once per track
# change, so no per-block file I/O is added. Falls back to a
# NullHandler if the data dir isn't writable, so logging never raises
# on the audio thread.
audio_log = logging.getLogger("tideway.audio")
audio_log.setLevel(logging.INFO)
audio_log.propagate = False
if not audio_log.handlers:
    try:
        from logging.handlers import RotatingFileHandler
        from app.paths import user_data_dir

        _ah = RotatingFileHandler(
            str(user_data_dir() / "audio.log"),
            maxBytes=1_000_000,
            backupCount=3,
        )
        _ah.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        audio_log.addHandler(_ah)
    except Exception:
        audio_log.addHandler(logging.NullHandler())


# Back-pressure between the decoder and the audio callback. Each
# item is one AudioFrame's worth of PCM (~1024 samples typical).
# Sized to hold ~2s of audio at 44.1k, ~1s at 96k, ~0.5s at 192k —
# enough headroom that a single slow segment fetch (TLS handshake,
# CDN edge swap, brief network blip) drains the queue without
# starving the audio callback. Earlier sizing of 30 was tight for
# CD-rate (0.7s) and dangerously tight for hi-res (0.16s at 192k),
# which produced audible stutter on the slower segment fetches
# hi-res implies (3-7 MB per segment vs. 0.5-1 MB for lossless).
# On older driver stacks (Windows 10 LTSB) sustained underruns
# can escalate to a fatal native abort; the bigger queue is the
# real fix and the diagnostic logging in _audio_callback is the
# safety net.
# Memory cost is negligible (~800 KB at int32 stereo).
_PCM_QUEUE_MAX = 100

# Minimum number of decoded chunks the incoming (preloaded) track must
# have buffered before a crossfade may begin, so the fade-in side doesn't
# immediately starve. If the preload isn't this far ahead yet, the
# crossfade simply doesn't start this callback and we re-check on the
# next one (and if the window closes first, it falls back to the gapless
# cut). A handful of chunks is a fraction of a second of audio.
_XFADE_MIN_PREBUFFER_CHUNKS = 4

# Hard ceiling on the two Tidal round-trips in _resolve_source. The
# transport already bounds each request (app.http.DEFAULT_TIMEOUT,
# ~36s worst case), so this is a backstop: it guarantees the player
# pipeline lock is released even if a transport ever fails to honour
# its own timeout. Sits above the transport ceiling so the
# transport's own, more specific error surfaces first in the normal
# case. Without it, one stalled request held the pipeline lock
# forever and every playback control hung until an app restart.
_RESOLVE_TIMEOUT_S = 45.0


@dataclass
class StreamInfo:
    """What's actually audible — drives the now-playing quality badge."""
    source: str  # "stream" | "local"
    codec: Optional[str] = None
    bit_depth: Optional[int] = None
    sample_rate_hz: Optional[int] = None
    audio_quality: Optional[str] = None
    audio_mode: Optional[str] = None
    # ReplayGain metadata pulled from the tidalapi Stream object.
    # Tidal masters come pre-tagged with both track-level and album-
    # level loudness offsets (relative to the EBU R128 reference) and
    # the actual sample peaks. The audio engine uses these to apply
    # loudness leveling at playback time when the user enables it.
    # All four are Optional because not every stream carries them
    # (older catalog, low-quality tier, edge cases).
    track_replay_gain_db: Optional[float] = None
    track_peak: Optional[float] = None
    album_replay_gain_db: Optional[float] = None
    album_peak: Optional[float] = None


@dataclass
class _Preload:
    """A pre-decoded next track. Created by `preload()` while the
    current track still has time to play. At end-of-current-track
    the audio callback tries to splice this in without pausing the
    OutputStream — if `sample_rate` and `sd_dtype` match the
    current stream, the swap is sample-accurate and the user hears
    no gap between tracks.
    """
    track_id: str
    quality: Optional[str]
    duration_ms: int
    stream_info: StreamInfo
    source_urls: Optional[list[str]]
    source_path: Optional[str]
    decoder: Decoder
    queue: "queue.Queue[Optional[np.ndarray]]"
    thread: threading.Thread
    stop_flag: threading.Event
    done: threading.Event
    sample_rate: int
    channels: int
    sd_dtype: str


@dataclass
class PlayerSnapshot:
    state: str  # idle | loading | playing | paused | ended | error
    track_id: Optional[str]
    position_ms: int
    duration_ms: int
    volume: int  # 0..100
    muted: bool
    error: Optional[str] = None
    seq: int = 0
    stream_info: Optional[StreamInfo] = None
    # Force Volume: when true the volume slider in the UI should
    # render disabled (value pinned at 100) because the backend
    # rejects changes while it's on.
    force_volume: bool = False


def _looks_like_auth_error(exc: BaseException) -> bool:
    """Best-effort detection of a Tidal auth (expired-token) error.

    Mirrors server._looks_like_401 deliberately — duplicated rather
    than imported because player.py must not import server (server
    imports player). tidalapi wraps these as requests.HTTPError with
    .response.status_code 401/403, or surfaces them as a RuntimeError
    whose str() carries '401' / 'Unauthorized'."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (401, 403):
        return True
    msg = str(exc)
    return "401" in msg or "Unauthorized" in msg


class PCMPlayer:
    def __init__(
        self,
        session_getter: Callable[[], tidalapi.Session],
        local_lookup: Optional[Callable[[str], Optional[str]]] = None,
        quality_clamp: Optional[Callable[[str], Optional[str]]] = None,
        force_refresh: Optional[Callable[[], bool]] = None,
    ):
        self._session_getter = session_getter
        self._local_lookup = local_lookup
        self._quality_clamp = quality_clamp
        # Called when a playback resolve hits an expired Tidal token.
        # Returns True if the token was refreshed (caller retries
        # once), False if the refresh token itself is dead (caller
        # surfaces a clear "log in again"). Injected by the server
        # the same way session_getter is; None in tests/standalone.
        self._force_refresh = force_refresh

        self._lock = threading.RLock()
        # Serializes load/stop/seek/preload so concurrent HTTP calls
        # can't race each other into opening overlapping streams or
        # sharing a single decoder across threads. Held for the full
        # duration of a state-changing operation; the audio callback
        # and decoder threads never touch it. RLock so play_track()
        # can take the lock around its load() + play() pair without
        # needing a separate locked helper for play.
        self._pipeline_lock = threading.RLock()
        self._state = "idle"
        self._current_track_id: Optional[str] = None
        self._current_duration_ms: int = 0
        self._current_stream_info: Optional[StreamInfo] = None
        self._last_error: Optional[str] = None
        self._seq = 0
        self._listeners: list[Callable[[PlayerSnapshot], None]] = []

        self._decoder: Optional[Decoder] = None
        self._stream: Optional[sd.OutputStream] = None
        self._stream_sample_rate: Optional[int] = None
        self._pcm_queue: queue.Queue[Optional[np.ndarray]] = queue.Queue(
            maxsize=_PCM_QUEUE_MAX
        )
        self._decoder_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._decoder_done = threading.Event()
        self._callback_carry: Optional[np.ndarray] = None
        self._samples_emitted = 0

        self._paused = False
        self._seeking = False
        # Set while _swap_pipeline_to() is rewriting the pipeline refs
        # from another thread. The audio callback is lock-free, so
        # without this it can run mid-swap and apply the previous
        # track's ReplayGain/filter state to the new track's samples —
        # a split second of full-scale clipped audio on track change.
        # Callback emits silence while this is set, same as _seeking.
        self._swapping = False
        self._volume = 100  # 0..100
        self._muted = False
        # External output active: when something else is rendering
        # the audio (Cast device, Tidal Connect target), the local
        # sounddevice OutputStream still runs but
        # writes silence so the user doesn't hear two copies of
        # the music coming from their Mac speakers and the remote
        # device. The PCM tap to those external sinks happens BEFORE
        # this silencing (see audio_callback) so the receivers still
        # get full-amplitude audio at their own volume control.
        # Flag is owned by the various external-output managers
        # (cast.py + tidal_connect.py both flip it on connect /
        # disconnect via the registered silencer hook).
        self._external_output_active: bool = False
        # sounddevice device-index string ("" = system default).
        # Applied on the next _open_output_stream(). Device-switch
        # live is a Phase 6 refinement.
        self._selected_device_id: str = ""
        # Exclusive Mode: request bit-perfect output from the OS audio
        # layer. Applied on the next _open_output_stream() so toggling
        # mid-track waits for the next stream open.
        self._exclusive_mode: bool = False
        # Force Volume: pin software volume at 100; set_volume becomes
        # a no-op while this is on. User attenuates via DAC/OS instead
        # so bit-depth isn't scaled away before the stream leaves us.
        self._force_volume: bool = False
        # Set True around any code path that intentionally stops the
        # current OutputStream because it's about to open a fresh
        # one in its place (set_output_device, exclusive-mode flip,
        # cross-rate gapless bridge). The finished_callback that
        # sounddevice fires from `stream.stop()` checks this flag and
        # skips its end-of-track / device-loss logic so a transient
        # stream replacement doesn't show up to the frontend as
        # `state="ended"` or trigger device-loss recovery against
        # a perfectly healthy device.
        self._replacing_stream: bool = False

        # Remembered source for restart-based seek. For DASH streams
        # we store the full URL list (index 0 = init segment, 1+ =
        # media segments) so seek can build a new SegmentReader
        # starting at the nearest segment. For local files we store
        # the filesystem path. `None` when no track is loaded.
        self._source_urls: Optional[list[str]] = None
        self._source_path: Optional[str] = None
        self._current_track_meta: Optional[dict] = None

        # Sounddevice dtype the current OutputStream opened with.
        # Stored so the gapless-swap check can verify the preloaded
        # track matches.
        self._stream_sd_dtype: Optional[str] = None
        self._stream_channels: Optional[int] = None

        # Preloaded next track. Populated by `preload()` ~15s before
        # the current track ends; consumed in-place by the audio
        # callback at end-of-track.
        self._preload: Optional[_Preload] = None

        # Set by _try_gapless_swap() when the audio callback
        # transitions to a new track while UPnP passthrough is active.
        # The callback can't block on HTTP calls (SetAVTransportURI),
        # so it defers to _load_locked() Path 0 which reads this flag
        # and calls start_passthrough for the new track.
        self._pending_upnp_notify: bool = False

        # Stream-manifest cache. Keyed by (track_id, quality_or_None).
        # Frontend hover / album-mount prefetch writes here so the
        # first real click is free of the playbackinfo round-trip
        # and, if bytes are warmed, of the init + first-media
        # segment fetches too. See app/audio/manifest_cache.py.
        self._manifest_cache = ManifestCache()

        # 10-band biquad EQ applied in the audio callback. One
        # instance owned for the lifetime of the player; when the
        # output stream rate changes (cross-rate bridge) we rebuild
        # it so the filter coefficients are recomputed against the
        # new sample rate. Remembered user settings (bands / preamp)
        # survive the rebuild.
        self._eq: Optional[Equalizer] = None
        # Manual parametric EQ — the user's editable band list +
        # master preamp. Remembered (not just the compiled SOS) so a
        # stream reopen recompiles coefficients at the new sample
        # rate without losing the curve.
        self._eq_parametric_bands: list[ParametricBand] = []
        self._eq_preamp: Optional[float] = None
        # AutoEQ headphone-profile mode (see
        # docs/autoeq-headphone-profiles-scope.md). Mutually
        # exclusive with `_eq_parametric_bands` — switching modes clears
        # whichever isn't active. Held here (rather than just an
        # SOS) so the stream-reopen path can recompile coefficients
        # at the new sample rate.
        self._eq_profile = None  # type: ignore[var-annotated]
        # Phase 4 A/B bypass — momentary disable that preserves
        # the active SOS / bands so flipping back is instant. The
        # audio callback reads this each frame; toggling has the
        # same ~immediate effect as a coefficient-clear without
        # the cost of rebuilding when the user toggles back on.
        self._eq_bypass: bool = False
        # Phase 5 user-tilt — bass / treble shelves + preamp
        # offset stacked on top of the active profile. Held as a
        # separate config so changing tilt without re-picking the
        # profile is one rebuild. None = no tilt set yet (treat
        # as flat); a TiltConfig with all-zero gains is also flat.
        self._eq_tilt = None  # type: ignore[var-annotated]
        # Bauer-style crossfeed for headphones — bleeds the low
        # frequencies of each channel into the opposite ear so hard-
        # panned mixes don't sound aggressive on cans. One instance
        # per output stream, rebuilt alongside the EQ on cross-rate
        # bridges. `_crossfeed_amount` (0-100 percent) is the user
        # setting we re-apply to a freshly-built filter.
        self._crossfeed: Optional[Crossfeed] = None
        self._crossfeed_amount: int = 0
        # ReplayGain loudness leveling. Single shared instance —
        # internal state is just a linear gain scalar that gets
        # recomputed whenever the active stream's tags or the user's
        # mode/preamp change. Off by default (gain_linear == 1.0).
        self._replaygain = ReplayGain()
        self._replaygain_mode: ReplayGainMode = "off"
        self._replaygain_preamp_db: float = 0.0
        self._replaygain_prevent_clipping: bool = True
        # Crossfade duration in seconds for automatic track-to-track
        # transitions (0 = off). The audio callback drives the actual
        # fade once a track nears its end and a compatible preload is
        # ready; when this is 0 that path is never entered, so playback
        # is exactly the existing gapless behaviour.
        self._crossfade_s: float = 0.0
        # In-progress fade state, owned by the audio callback. `_xfade_in`
        # is the _Preload we've claimed as the incoming track; `_xfade_pos`
        # / `_xfade_total` track the fade in samples; `_xfade_carry_in` is
        # the incoming queue's partial-chunk carry (mirrors
        # `_callback_carry` for the outgoing side).
        self._xfade_active: bool = False
        self._xfade_pos: int = 0
        self._xfade_total: int = 0
        self._xfade_in: Optional[_Preload] = None
        self._xfade_carry_in: Optional[np.ndarray] = None

        # Audio-callback diagnostics. Each pair is (count, last-print
        # time) for a different rate-limited stderr message:
        #   _cb_status: PortAudio over/underrun flags from the
        #     callback's `status` arg (driver-level glitch).
        #   _cb_starve: our decoder didn't push a chunk in time
        #     (our-side fault — slow disk / network / CPU).
        # Initialised here rather than lazy-init via getattr in the
        # callback so the attribute names live in one place and a
        # typo at the use site is a fail-fast AttributeError, not a
        # silently-zeroed counter.
        self._cb_status_count = 0
        self._cb_status_last_print = 0.0
        self._cb_starve_count = 0
        self._cb_starve_last_print = 0.0
        # Callback-jitter detection. PortAudio invokes the callback on
        # a realtime thread roughly every frames/rate seconds. When
        # something hogs the GIL/CPU (a page-navigation enrichment
        # burst, big JSON serialize) the callback is delivered late
        # even though the PCM queue is full — audible crackle that
        # never trips queue-starvation or PortAudio's own underrun
        # flag. We time the gap between consecutive audio-delivering
        # callbacks and log a missed deadline, rate-limited.
        self._cb_last_t = 0.0
        self._cb_prev_audio = False
        self._cb_jitter_count = 0
        self._cb_jitter_last_print = 0.0
        self._cb_jitter_worst_ms = 0.0
        # Cumulative (lifetime) totals of the same three glitch classes,
        # surfaced in the activity report so a stutter bug can be triaged
        # without anyone hunting for the rate-limited stderr / audio.log
        # lines. The per-second counters above reset each second; these
        # never do. Each maps to a distinct cause: status → driver /
        # exclusive-mode / device; starvation → our decode/network
        # couldn't keep up; jitter → GIL/CPU contention elsewhere.
        self._cb_status_total = 0
        self._cb_starve_total = 0
        self._cb_jitter_total = 0
        self._cb_jitter_worst_late_ms = 0.0
        # Heartbeat tick counter, only incremented when _DEBUG is on.
        # 1-per-callback log every 100 ticks confirms the callback
        # is actually running + advancing.
        self._callback_counter = 0
        # Last-bump timestamp for the audio callback's position-tick
        # nudges. The callback bumps `_seq` from here on a wall-clock
        # cadence (~5 Hz) so the SSE polling rate (4 Hz) always sees
        # a fresh seq, regardless of how often the callback itself
        # fires. The earlier "every 20 callbacks" scheme assumed an
        # 86 Hz callback rate (44.1k / 512 frames); at lower callback
        # rates — large buffers, hi-res-output configurations,
        # exclusive-mode WASAPI on devices that prefer 2048-frame
        # buffers — seq fell behind 4 Hz, the frontend's seq-dedup
        # in usePlayer kicked in, and the scrubber updated only
        # every couple of seconds instead of every 250 ms.
        self._seq_last_bump_t: float = 0.0

        # Stall watchdog. The realtime callback updates _cb_last_t
        # on every invocation (even paused/seeking — it still fires
        # to output silence), so if _cb_last_t goes stale while we
        # should be playing, the callback thread is wedged (the
        # silent-freeze class that left no log line and needed a
        # manual `sample` to diagnose). The watchdog is deliberately
        # lock-free so it can still fire while _lock is deadlocked,
        # and it dumps every thread's stack so the next freeze
        # diagnoses itself.
        self._stall_dumped = False
        # Wall-clock the watchdog uses to time how long the player has
        # sat in "loading"; reset whenever the state isn't loading. A
        # load that never produces audio (wedged segment fetch, dead
        # stream id) would otherwise pin the UI in "loading" forever.
        self._loading_since: Optional[float] = None
        threading.Thread(
            target=self._stall_watchdog,
            name="player-stall-watchdog",
            daemon=True,
        ).start()

        # macOS audio-route listener. Subscribes to TWO CoreAudio
        # events:
        #   1. Default output device changed (headphones unplug
        #      auto-reroutes to speakers; user picks a different
        #      device in Sound prefs).
        #   2. Device list changed (a device plugged in or out,
        #      independent of whether it became the default).
        # Both feed the same handler — recovery is idempotent and
        # the player's state checks naturally dedupe.
        #
        # The dual subscription is important for "headphones
        # plugged in but macOS didn't auto-route to them" (a
        # supported Sound-pref configuration). Without the
        # device-list listener, PortAudio's enumeration stays
        # stale after that plug-in event and the picker never
        # shows the new headphones until the user restarts.
        self._audio_route_listener_unregister: Optional[
            Callable[[], None]
        ] = None
        if sys.platform == "darwin":
            try:
                from app.audio.macos_audio_devices import (
                    register_audio_route_listener,
                )
                self._audio_route_listener_unregister = (
                    register_audio_route_listener(
                        self._on_audio_route_changed
                    )
                )
                if self._audio_route_listener_unregister is not None:
                    print(
                        "[audio] subscribed to CoreAudio default-output "
                        "+ device-list changes",
                        flush=True,
                    )
            except Exception:
                log.exception(
                    "audio-route listener registration crashed"
                )

    def _on_audio_route_changed(self) -> None:
        """Fired (on a CoreAudio thread) whenever the audio routing
        topology changes — default output device flipped OR a
        device plugged in / unplugged.

        We respond with `_recover_from_device_loss` regardless of
        which event fired. The recovery is dual-purpose: when the
        selected device disappeared it falls back to default, when
        the selected device is still around it reopens on the
        original (preserving the user's manual selection AND
        refreshing PortAudio's enumeration so a newly-plugged-in
        device shows up in the picker).

        Idempotent: if the player is idle / error / ended, recovery
        short-circuits and we just need PortAudio to re-enumerate
        on the next picker open.

        Spawned on a fresh thread because we're on a CoreAudio
        internal thread here and shouldn't re-enter stream
        machinery (PortAudio reinit specifically would deadlock).
        """
        with self._lock:
            state = self._state
        if state not in ("playing", "paused", "loading"):
            # No active stream. The picker will refresh PortAudio
            # itself the next time the user opens it, so there's
            # nothing to do here.
            return
        print(
            f"[audio] audio route changed (state={state!r}); "
            "kicking recovery",
            flush=True,
        )
        threading.Thread(
            target=self._recover_from_device_loss,
            name="pcm-audio-route-changed",
            daemon=True,
        ).start()

    # --- public API -------------------------------------------------

    def subscribe(
        self, listener: Callable[[PlayerSnapshot], None]
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return _unsub

    def load(
        self, track_id: str, quality: Optional[str] = None
    ) -> PlayerSnapshot:
        """Resolve stream/local source, open decoder + output stream.
        Doesn't start playback; caller must `play()` next.

        Serialized on `_pipeline_lock`: concurrent HTTP calls (rapid
        skip, backend auto-swap colliding with frontend play_track,
        etc.) resolve in order rather than racing into overlapping
        sounddevice streams.
        """
        with self._pipeline_lock:
            return self._load_locked(track_id, quality)

    def _load_locked(
        self, track_id: str, quality: Optional[str]
    ) -> PlayerSnapshot:
        self._dbg(
            f"load track={track_id} quality={quality} "
            f"current_track={self._current_track_id} "
            f"current_state={self._state} "
            f"preload={self._preload.track_id if self._preload else 'None'}"
        )
        with self._lock:
            # Path 0: already audible on this track. Happens when
            # the callback's gapless swap already transitioned us
            # to the next track and the frontend's redundant
            # play_track() lands here. Don't tear anything down —
            # we're already playing what the caller asked for.
            if (
                self._current_track_id == track_id
                and self._state in ("playing", "paused")
            ):
                # Deferred UPnP passthrough notification from
                # _try_gapless_swap (audio callback can't do HTTP).
                # Start passthrough for the new track so the
                # renderer receives SetAVTransportURI before it
                # hits EOF on the previous track's buffer.
                if self._pending_upnp_notify:
                    self._pending_upnp_notify = False
                    if (
                        _upnp_manager is not None
                        and _upnp_manager.is_active()
                        and isinstance(self._source_urls, list)
                    ):
                        try:
                            _upnp_manager.start_passthrough(
                                self._source_urls,
                                prefetched=None,
                                metadata=self._current_track_meta,
                            )
                        except (ValueError, RuntimeError, OSError) as exc:
                            print(
                                f"[player] upnp start_passthrough failed "
                                f"(Path 0): {exc!r}",
                                flush=True,
                            )
                self._dbg(f"load Path 0: already playing track={track_id}")
                return self.snapshot()
            # Path 0b: a preload for this track is already buffering.
            # Adopt it without tearing down the sounddevice stream.
            # When rates + dtype match, the active OutputStream just
            # starts being fed from the preload's queue instead —
            # which is the whole point of the preload. This is the
            # path that keeps auto-advance gapless when the frontend
            # races the callback's swap.
            pre = self._preload
            if (
                pre is not None
                and pre.track_id == track_id
                and (quality is None or pre.quality == quality)
                and self._state in ("playing", "paused", "loading")
            ):
                self._dbg(
                    f"load Path 0b: adopting preload track={track_id} "
                    f"(rate {pre.sample_rate}=={self._stream_sample_rate}? "
                    f"dtype {pre.sd_dtype}=={self._stream_sd_dtype}?)"
                )
                return self._adopt_preload_locked(pre)
        # Fall through: no matching preload.
        #
        # Two slow paths from here:
        #
        #  (A) **Gapless swap** when there is an active OutputStream and
        #      the new decoder can produce samples in the same rate /
        #      dtype / channel triple as the active stream. The new
        #      decoder thread runs against a fresh queue, then we
        #      atomically swap references the way `_adopt_preload_locked`
        #      does for a real preload — the OutputStream stays open
        #      across the swap, so we never race the OS releasing the
        #      audio device. This is critical on Windows audio devices
        #      with slow `IAudioClient::Release` (USB DACs are the
        #      canonical case: their second `stream.start()` after a
        #      `close()` fails with a `WdmSyncIoctl` IOCTL error, the
        #      player flips to state=error, and the frontend's
        #      auto-advance burns through the queue at ~200 ms / track).
        #
        #  (B) **Full teardown + fresh open** when no stream is open yet
        #      (cold start, after a stop), the device id changed, or the
        #      new decoder's format doesn't fit the active stream. This
        #      is the original slow path and the race-prone one — but
        #      it only fires in cases where we genuinely have to close
        #      and reopen.
        self._dbg(f"load SLOW PATH: full teardown + fresh resolve for {track_id}")
        self._pending_upnp_notify = False
        load_t0 = time.monotonic()

        # Snapshot the active stream's format so we can decide whether
        # path (A) is viable BEFORE doing any teardown.
        with self._lock:
            active_stream = self._stream
            active_rate = self._stream_sample_rate
            active_dtype = self._stream_sd_dtype
            active_channels = self._stream_channels

        if active_stream is not None and active_rate is not None:
            try:
                with self._lock:
                    self._transition("loading", track_id=track_id)
                synthetic = self._build_load_pipeline(
                    track_id, quality, match_rate=active_rate
                )
                t_built = time.monotonic()
            except Exception as exc:
                log.exception(
                    "failed to build pipeline for gapless swap on %s", track_id
                )
                with self._lock:
                    self._last_error = str(exc)
                    self._transition("error")
                self._emit()
                return self.snapshot()

            rate_matches = (
                synthetic.sample_rate == active_rate
                and synthetic.sd_dtype == active_dtype
                and synthetic.channels == active_channels
            )
            if rate_matches:
                # Path (A): atomic swap, keep stream open. Mirrors
                # `_adopt_preload_locked`'s same-rate branch.
                with self._lock:
                    old_thread = self._decoder_thread
                    old_stop_flag = self._stop_flag
                    old_decoder = self._decoder
                    # Drop any stale preload (its decoder thread won't
                    # ever be adopted now that we've replaced the
                    # active pipeline with a new track).
                    stale_preload = self._preload
                    self._preload = None
                    self._swap_pipeline_to(synthetic)
                    self._source_urls = synthetic.source_urls
                    self._source_path = synthetic.source_path
                    self._paused = False
                    self._seeking = False
                    self._last_error = None
                    # Same contract as the original slow path: load()
                    # leaves us in "paused"; caller's play() flips to
                    # "playing".
                    self._transition("paused")

                # Outside the lock: clean up old refs without holding it
                # while we wait on a thread join.
                if old_stop_flag is not None:
                    old_stop_flag.set()
                if old_decoder is not None:
                    try:
                        old_decoder.cancel_source()
                    except Exception:
                        pass
                if old_thread is not None and old_thread.is_alive():
                    old_thread.join(timeout=0.5)
                if old_decoder is not None:
                    try:
                        old_decoder.close()
                    except Exception:
                        pass
                # Stale preload: stop its thread and close its decoder
                # so we don't leak. cancel_source aborts any in-flight
                # HTTP fetch so the join returns promptly.
                if stale_preload is not None:
                    stale_preload.stop_flag.set()
                    try:
                        stale_preload.decoder.cancel_source()
                    except Exception:
                        pass
                    if stale_preload.thread.is_alive():
                        stale_preload.thread.join(timeout=0.5)
                    try:
                        stale_preload.decoder.close()
                    except Exception:
                        pass

                _perf = (
                    f"[perf] load track={track_id} "
                    f"total={(time.monotonic() - load_t0) * 1000.0:.0f}ms "
                    f"build={(t_built - load_t0) * 1000.0:.0f}ms "
                    f"swap={(time.monotonic() - t_built) * 1000.0:.0f}ms "
                    f"(gapless: kept stream open)"
                )
                print(_perf, file=sys.stderr, flush=True)
                # Also persist: a Finder-launched .app discards stderr,
                # so without this the slow-start breakdown is lost.
                audio_log.info(_perf)
                self._emit()
                return self.snapshot()
            else:
                # Format mismatch — the synthetic pipeline's decoder
                # can't feed the active stream. Tear it down and fall
                # through to path (B), which closes the old stream and
                # opens a new one at the new format. The close+open
                # race can still fire here, but format-change track
                # changes are far rarer than format-match ones.
                self._dbg(
                    f"load slow path: format mismatch "
                    f"({synthetic.sample_rate}/{synthetic.sd_dtype}/"
                    f"{synthetic.channels} vs active "
                    f"{active_rate}/{active_dtype}/{active_channels}); "
                    f"discarding synthetic pipeline"
                )
                synthetic.stop_flag.set()
                try:
                    synthetic.decoder.cancel_source()
                except Exception:
                    pass
                if synthetic.thread.is_alive():
                    synthetic.thread.join(timeout=0.5)
                try:
                    synthetic.decoder.close()
                except Exception:
                    pass

        # Path (B): no active stream (or format mismatch fell through).
        # Full teardown + fresh open at the new decoder's native rate.
        self._teardown()
        t_teardown = time.monotonic()
        with self._lock:
            self._transition("loading", track_id=track_id)

        try:
            source_spec, duration_s, stream_info, prefetched_bytes = self._resolve_source(
                track_id, quality
            )
            t_resolved = time.monotonic()
            initial_source = _build_source(source_spec, prefetched=prefetched_bytes)

            # Start DLNA passthrough if a session is active. Same
            # condition as the _build_load_pipeline path above.
            if _upnp_manager is not None and isinstance(source_spec, list):
                try:
                    if _upnp_manager.is_active():
                        _upnp_manager.start_passthrough(
                            source_spec,
                            prefetched=prefetched_bytes,
                            metadata=self._current_track_meta,
                        )
                except (ValueError, RuntimeError, OSError) as exc:
                    print(
                        f"[player] upnp start_passthrough failed "
                        f"(Path B): {exc!r}",
                        flush=True,
                    )

            decoder = Decoder(initial_source)
            t_decoder = time.monotonic()
        except Exception as exc:
            log.exception("failed to resolve/open source for %s", track_id)
            with self._lock:
                self._last_error = str(exc)
                self._transition("error")
            self._emit()
            return self.snapshot()

        with self._lock:
            self._decoder = decoder
            self._current_duration_ms = (
                int(duration_s * 1000) if duration_s else 0
            )
            self._current_stream_info = stream_info
            self._stream_sample_rate = decoder.sample_rate
            self._stream_sd_dtype = decoder.sounddevice_dtype
            self._stream_channels = decoder.channels
            if isinstance(source_spec, str):
                self._source_path = source_spec
                self._source_urls = None
            else:
                self._source_urls = list(source_spec)
                self._source_path = None
            t_before_stream = time.monotonic()
            self._open_output_stream(
                decoder.sample_rate, decoder.channels, decoder.sounddevice_dtype
            )
            t_stream_open = time.monotonic()
            self._samples_emitted = 0
            self._callback_carry = None
            self._paused = False
            self._seeking = False
            # Fresh events + queue for the new run. An old decoder
            # thread whose join() timed out might still be alive
            # holding the PREVIOUS objects as locals. If we reuse
            # those, the zombie thread would keep pushing pre-load
            # samples into the live queue and see its stop_flag
            # toggle back to "not set," so we hand the new run a
            # clean set of objects.
            self._stop_flag = threading.Event()
            self._decoder_done = threading.Event()
            self._pcm_queue = queue.Queue(maxsize=_PCM_QUEUE_MAX)
            self._last_error = None
            # Transition out of "loading" now that the decoder is up
            # and the output stream is open. We land in "paused"
            # because nothing has called .play() yet — this is the
            # "loaded but not playing" state. play_track() will move
            # us to "playing" via its play() call right after this
            # returns; load()-without-play callers (the restore-on-
            # quit path, the album-end pause-on-track-1 flow) want
            # the snapshot to report "paused" here, not a perpetual
            # "loading" the frontend can't recover from.
            self._transition("paused")

        # Re-derive ReplayGain from the new stream's tags. The
        # helper is lock-free, so it doesn't matter that we're now
        # outside the load lock; doing it synchronously keeps the
        # gain stage in lockstep with the now-playing track instead
        # of waiting for the first audio callback to notice.
        self._apply_replaygain_for(self._current_stream_info)

        self._start_decoder_thread()
        t_thread_started = time.monotonic()
        # Emit the full breakdown at the bottom so all phases are on
        # one line. Each segment in milliseconds, total for the
        # click-to-loaded path. The two phases users care about most
        # are `resolve` (Tidal API roundtrip) and `decoder_init`
        # (manifest fetch + first segment fetch + libav probe).
        # `stream_open` is the sounddevice OutputStream open and
        # `thread_start` is the producer-thread spawn cost; both
        # stable across calls so they're floor values.
        _perf = (
            f"[perf] load track={track_id} "
            f"total={(t_thread_started - load_t0) * 1000.0:.0f}ms "
            f"teardown={(t_teardown - load_t0) * 1000.0:.0f}ms "
            f"resolve={(t_resolved - t_teardown) * 1000.0:.0f}ms "
            f"decoder_init={(t_decoder - t_resolved) * 1000.0:.0f}ms "
            f"stream_open={(t_stream_open - t_before_stream) * 1000.0:.0f}ms "
            f"thread_start="
            f"{(t_thread_started - t_stream_open) * 1000.0:.0f}ms"
        )
        print(_perf, file=sys.stderr, flush=True)
        # Also persist: a Finder-launched .app discards stderr, so
        # without this the cold-start phase breakdown is unrecoverable.
        audio_log.info(_perf)
        self._emit()
        return self.snapshot()

    def play_track(
        self, track_id: str, quality: Optional[str] = None
    ) -> PlayerSnapshot:
        """Combined load + play. The pipeline lock keeps load +
        play atomic relative to other state changes, so a
        concurrent stop / seek can't land in between the two
        phases."""
        with self._pipeline_lock:
            snap = self.load(track_id, quality=quality)
            if snap.state == "error":
                return snap
            return self.play()

    def play(self) -> PlayerSnapshot:
        with self._lock:
            stream = self._stream
            if stream is None:
                return self.snapshot()
            self._paused = False
            try:
                if not stream.active:
                    stream.start()
            except Exception as exc:
                log.exception("stream.start failed")
                self._last_error = str(exc)
                self._transition("error")
                self._emit()
                return self.snapshot()
            self._transition("playing")
        self._emit()
        return self.snapshot()

    def pause(self) -> PlayerSnapshot:
        with self._lock:
            if self._state not in ("playing", "loading"):
                return self.snapshot()
            self._paused = True
            self._transition("paused")
        self._emit()
        return self.snapshot()

    def resume(self) -> PlayerSnapshot:
        return self.play()

    def stop(self) -> PlayerSnapshot:
        with self._pipeline_lock:
            self._teardown()
            with self._lock:
                self._last_error = None
            self._emit()
            return self.snapshot()

    def seek(self, fraction: float) -> PlayerSnapshot:
        """Seek to `fraction` (0..1) of the track duration.

        libav's DASH/fragmented-MP4 seek doesn't work without an
        MPD-derived index, so we can't use `container.seek()` for
        Tidal streams. Instead we rebuild the decoder with a new
        `SegmentReader` that starts at the nearest media segment
        to `target_s`. Seek accuracy is segment-granular (~3-4s
        per segment at hi-res); close enough for scrub UX.

        Local files get a precise seek via `container.seek()` on
        the newly-opened container.
        """
        with self._pipeline_lock:
            fraction = max(0.0, min(1.0, float(fraction)))
            with self._lock:
                duration_ms = self._current_duration_ms
                sample_rate = self._stream_sample_rate or 44100
                if duration_ms <= 0:
                    return self.snapshot()
                target_s = (duration_ms / 1000.0) * fraction
            self._dbg(
                f"seek ENTER fraction={fraction} target_s={target_s:.2f} "
                f"duration_ms={duration_ms} sample_rate={sample_rate} "
                f"samples_emitted_before={self._samples_emitted}"
            )

            # Suppress CallbackStop during the decoder swap. Without
            # this, the brief window where the queue is empty AND the
            # old decoder has exited would let the callback raise
            # CallbackStop and end the stream mid-seek.
            with self._lock:
                self._seeking = True
            # Seeking inside the crossfade window invalidates the fade —
            # the position the user jumped to bears no relation to the
            # in-progress transition. `_seeking` now silences the callback,
            # so cancel the fade and dispose the claimed incoming preload.
            self._abort_crossfade()
            # Stop DLNA passthrough on seek so the encoder doesn't
            # keep streaming from the pre-seek position. The PCM
            # re-encode path (FlacStreamEncoder via push_pcm) takes
            # over for the remainder of this track; passthrough
            # resumes automatically when the next track loads.
            # See issue #273 for context.
            if _upnp_manager is not None:
                try:
                    if _upnp_manager.is_active():
                        _upnp_manager.stop_passthrough()
                except Exception as exc:
                    print(f"[player] upnp stop_passthrough on seek failed: {exc!r}", flush=True)
            effective_s = target_s
            try:
                effective_s = self._restart_decoder_at(target_s)
            except Exception:
                log.exception("seek to %s failed", target_s)
            finally:
                with self._lock:
                    # Use the DECODER'S effective start, not the
                    # user's requested target, so position reporting
                    # matches the audio you actually hear. On DASH
                    # this may be up to one segment earlier than
                    # requested (~3-4s).
                    self._samples_emitted = int(effective_s * sample_rate)
                    self._seeking = False
                    self._seq += 1
            return self.snapshot()

    # --- restart-based seek -----------------------------------------

    def _restart_decoder_at(self, target_s: float) -> float:
        """Tear down the current decoder + thread; build a new
        Decoder starting at or near `target_s`; start a new thread.
        Returns the effective start offset in seconds (= target_s
        for local files, = approximate segment start for DASH —
        which may be up to one segment before target_s).
        """
        seek_t0 = time.monotonic()
        self._stop_flag.set()
        # Cancel the decoder's source BEFORE joining the thread.
        # `stop_flag` only gets checked between PCM frames; if the
        # decoder is mid-segment-fetch in SegmentReader (the common
        # case during user seeks because the queue is small) we'd
        # otherwise wait out the rest of the HTTP read — 150-300 ms
        # of "nothing happens" between click and audio. cancel_source
        # closes the in-flight Response so iter_content raises and
        # the thread exits in single-digit ms.
        old_decoder = self._decoder
        if old_decoder is not None:
            try:
                old_decoder.cancel_source()
            except Exception:
                pass
        thread = self._decoder_thread
        self._decoder_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        t_thread_joined = time.monotonic()

        self._decoder = None
        if old_decoder is not None:
            try:
                old_decoder.close()
            except Exception:
                pass
        t_old_closed = time.monotonic()

        new_source, start_offset_s = self._build_source_at(target_s)
        t_source_built = time.monotonic()
        new_decoder = Decoder(new_source)
        # Match the existing OutputStream's configuration. The stream
        # stays open across a seek; the OLD decoder was configured to
        # emit at `_stream_sample_rate` / matching dtype via the
        # set_target_rate call in _open_output_stream. The NEW
        # Decoder() defaults to source-rate / source-format output —
        # which may not match the stream when shared-mode resampling
        # is in play (e.g. 96 kHz FLAC stream open at the device's
        # 48 kHz mixer rate as float32, but the new decoder produces
        # int32 at 96 kHz). The audio_callback then writes int32
        # bytes into a buffer PortAudio reads as float32, which gets
        # reinterpreted at the wrong scale and sounds like blown-out
        # bit-crushed audio. set_target_rate flips the new decoder
        # into the same flt-at-target_rate config the old one had,
        # or no-ops in the passthrough case (exclusive mode, where
        # stream rate == source rate).
        if self._stream_sample_rate:
            new_decoder.set_target_rate(self._stream_sample_rate)
        t_decoder_init = time.monotonic()
        # For local files the new container starts at t=0; tell it
        # to seek forward to the precise target. For DASH we already
        # opened at the right segment, so the decoder starts near
        # target_s naturally.
        if start_offset_s is None:
            new_decoder.request_seek(target_s)
            effective_s = target_s
        else:
            effective_s = start_offset_s

        with self._lock:
            self._decoder = new_decoder
            self._callback_carry = None
            # Fresh events + queue — see comment in _load_locked().
            self._stop_flag = threading.Event()
            self._decoder_done = threading.Event()
            self._pcm_queue = queue.Queue(maxsize=_PCM_QUEUE_MAX)

        self._start_decoder_thread()
        t_thread_started = time.monotonic()

        # Same shape as the load() perf line so users / devs can read
        # both with the same eyes. `thread_join` covers waiting on
        # the previous decoder to exit (capped at 2 s — long values
        # here mean a stuck producer); `source_build` is the segment-
        # picking arithmetic for DASH or path lookup for local;
        # `decoder_init` is libav opening the new source. Stream
        # stays open across a seek so there's no `stream_open`
        # phase here.
        kind = "local" if start_offset_s is None else "dash"
        _perf = (
            f"[perf] seek target_s={target_s:.2f} kind={kind} "
            f"effective_s={effective_s:.2f} "
            f"total={(t_thread_started - seek_t0) * 1000.0:.0f}ms "
            f"thread_join={(t_thread_joined - seek_t0) * 1000.0:.0f}ms "
            f"old_close="
            f"{(t_old_closed - t_thread_joined) * 1000.0:.0f}ms "
            f"source_build="
            f"{(t_source_built - t_old_closed) * 1000.0:.0f}ms "
            f"decoder_init="
            f"{(t_decoder_init - t_source_built) * 1000.0:.0f}ms "
            f"thread_start="
            f"{(t_thread_started - t_decoder_init) * 1000.0:.0f}ms"
        )
        print(_perf, file=sys.stderr, flush=True)
        audio_log.info(_perf)
        return effective_s

    def _build_source_at(
        self, target_s: float
    ) -> tuple[Union[str, SegmentReader], Optional[float]]:
        """Return (source, approx_start_s). approx_start_s is None
        for local files (precise seek via container.seek) and the
        segment's approximate start time for DASH.
        """
        if self._source_urls is not None:
            urls = self._source_urls
            num_media = len(urls) - 1
            duration_s = (self._current_duration_ms or 1000) / 1000.0
            if target_s <= 0 or num_media == 0:
                return SegmentReader(urls), 0.0
            idx = max(
                0,
                min(num_media - 1, int(target_s / duration_s * num_media)),
            )
            approx_start_s = (idx / num_media) * duration_s
            # init segment + remaining media segments from idx
            sliced = [urls[0]] + list(urls[1 + idx:])
            return SegmentReader(sliced), approx_start_s
        if self._source_path is not None:
            return self._source_path, None
        raise RuntimeError("no source to seek within")

    _DEBUG = False  # verbose stderr logging — flip on while
    # debugging. At False only [pcm] state-transition prints fire.

    def _dbg(self, msg: str) -> None:
        if PCMPlayer._DEBUG:
            print(f"[pcm] {msg}", file=sys.stderr, flush=True)

    def preload(
        self, track_id: str, quality: Optional[str] = None
    ) -> dict:
        """Pre-decode the next track into a buffer so the audio
        callback can splice it into the current OutputStream at
        end-of-current-track — sample-accurate gapless.

        Idempotent for the same (track_id, quality); swapping the
        request to a different track tears down the existing
        preload first. Serialized on `_pipeline_lock` alongside
        load/stop/seek.
        """
        self._dbg(f"preload ENTER track={track_id} quality={quality}")
        with self._pipeline_lock:
            with self._lock:
                existing = self._preload
                if (
                    existing is not None
                    and existing.track_id == track_id
                    and existing.quality == quality
                ):
                    self._dbg(f"preload already cached for track={track_id}")
                    return {"ok": True, "cached": True, "hit": True}
            # Different track (or no preload) — tear down any stale
            # one before we build the new one.
            self._drop_preload()

            try:
                source_spec, duration_s, stream_info, prefetched_bytes = (
                    self._resolve_source(track_id, quality)
                )
                source = _build_source(source_spec, prefetched=prefetched_bytes)
                decoder = Decoder(source)
            except Exception as exc:
                log.exception("preload resolve failed for %s", track_id)
                return {"ok": False, "error": str(exc)}

            # Configure the preload's output rate to match the active
            # stream so the gapless splice works for any source-rate
            # combination. In shared mode that's the device's mixer
            # rate, so a preloaded 96 k track decoded against a 48 k
            # stream produces 48 k output and joins seamlessly. In
            # exclusive mode we keep the preload at its own source
            # rate; if it doesn't match the active stream, the adopt
            # path reopens the stream (same ~50 ms gap as before).
            current_stream_rate = self._stream_sample_rate
            if not self._exclusive_mode and current_stream_rate:
                decoder.set_target_rate(current_stream_rate)

            q: queue.Queue[Optional[np.ndarray]] = queue.Queue(
                maxsize=_PCM_QUEUE_MAX
            )
            stop_flag = threading.Event()
            done = threading.Event()

            thread = threading.Thread(
                target=PCMPlayer._decoder_loop,
                args=(decoder, q, stop_flag, done),
                name="pcm-preload",
                daemon=True,
            )
            thread.start()

            urls = source_spec if isinstance(source_spec, list) else None
            path = source_spec if isinstance(source_spec, str) else None

            self._dbg(
                f"preload READY track={track_id} "
                f"rate={decoder.output_sample_rate} dtype={decoder.sounddevice_dtype} "
                f"(current stream rate={self._stream_sample_rate} "
                f"dtype={self._stream_sd_dtype})"
            )
            pre = _Preload(
                track_id=track_id,
                quality=quality,
                duration_ms=int(duration_s * 1000) if duration_s else 0,
                stream_info=stream_info,
                source_urls=list(urls) if urls is not None else None,
                source_path=path,
                decoder=decoder,
                queue=q,
                thread=thread,
                stop_flag=stop_flag,
                done=done,
                sample_rate=decoder.output_sample_rate,
                channels=decoder.channels,
                sd_dtype=decoder.sounddevice_dtype,
            )
            with self._lock:
                self._preload = pre
            return {
                "ok": True,
                "cached": True,
                "sample_rate": pre.sample_rate,
                "dtype": pre.sd_dtype,
            }

    def _build_load_pipeline(
        self,
        track_id: str,
        quality: Optional[str],
        match_rate: Optional[int] = None,
    ) -> "_Preload":
        """Resolve `track_id`, open a decoder, start its producer
        thread, and wrap the lot as a `_Preload` for the caller to
        adopt via `_swap_pipeline_to`.

        Mirrors `preload()`'s build steps without the `_preload` slot
        assignment — the gapless load path uses this to manufacture a
        synthetic preload so it can keep the OutputStream open across a
        track change (avoiding the WASAPI `IAudioClient::Release` race
        on slow USB audio devices). `match_rate`, if provided, asks the
        decoder to emit at that rate so the swap stays format-compatible
        with the active stream; pass `None` to keep the decoder's native
        rate.

        Raises on resolve / decoder construction failures. Caller is
        responsible for tearing down the returned pipeline if it
        decides not to adopt it.
        """
        source_spec, duration_s, stream_info, prefetched_bytes = self._resolve_source(
            track_id, quality
        )
        source = _build_source(source_spec, prefetched=prefetched_bytes)

        # Start bit-perfect FLAC passthrough when a DLNA session is
        # active. The passthrough encoder demuxes raw FLAC frames
        # from the fMP4 source and writes them directly to the
        # ring buffer, bypassing PCM decode + re-encode so the
        # original STREAMINFO (with real total_samples) is preserved.
        # Only when the source is a URL list (Tidal stream), not a
        # local file.
        if _upnp_manager is not None and isinstance(source_spec, list):
            try:
                if _upnp_manager.is_active():
                    _upnp_manager.start_passthrough(
                        source_spec,
                        prefetched=prefetched_bytes,
                        metadata=self._current_track_meta,
                    )
            except (ValueError, RuntimeError, OSError) as exc:
                print(
                    f"[player] upnp start_passthrough failed: {exc!r}",
                    flush=True,
                )

        decoder = Decoder(source)

        # Match the active stream's rate so the adopt path can take
        # the same-rate (stream-stays-open) branch. Skipped in
        # exclusive mode because we want the source's native rate
        # there — if it ends up differing from the active stream's,
        # the caller falls through to the full teardown path.
        if match_rate is not None and not self._exclusive_mode:
            decoder.set_target_rate(int(match_rate))

        q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(
            maxsize=_PCM_QUEUE_MAX
        )
        stop_flag = threading.Event()
        done = threading.Event()

        thread = threading.Thread(
            target=PCMPlayer._decoder_loop,
            args=(decoder, q, stop_flag, done),
            name="pcm-decoder",
            daemon=True,
        )
        thread.start()

        urls = source_spec if isinstance(source_spec, list) else None
        path = source_spec if isinstance(source_spec, str) else None

        return _Preload(
            track_id=track_id,
            quality=quality,
            duration_ms=int(duration_s * 1000) if duration_s else 0,
            stream_info=stream_info,
            source_urls=list(urls) if urls is not None else None,
            source_path=path,
            decoder=decoder,
            queue=q,
            thread=thread,
            stop_flag=stop_flag,
            done=done,
            sample_rate=decoder.output_sample_rate,
            channels=decoder.channels,
            sd_dtype=decoder.sounddevice_dtype,
        )

    def _drop_preload(self) -> None:
        """Stop the preload thread, close its decoder, clear the slot.
        Safe to call from either a locked pipeline operation or from
        the HTTP layer — takes the pipeline lock itself (RLock, so
        nesting is fine).
        """
        with self._pipeline_lock:
            with self._lock:
                pre = self._preload
                self._preload = None
            if pre is None:
                return
            pre.stop_flag.set()
            # Abort any in-flight HTTP fetch on the preload's decoder
            # so its thread exits promptly instead of waiting out the
            # rest of a 150-300 ms segment download. Drop-preload
            # fires on every track change while a preload is buffering;
            # without this, a fast skip stalls behind the dead preload.
            try:
                pre.decoder.cancel_source()
            except Exception:
                pass
            if pre.thread.is_alive():
                pre.thread.join(timeout=2.0)
            try:
                pre.decoder.close()
            except Exception:
                pass

    def set_volume(self, volume: int) -> PlayerSnapshot:
        volume = max(0, min(100, int(volume)))
        with self._lock:
            # Force Volume wins over caller requests — keeps the
            # signal chain bit-perfect. UI disables the slider in
            # that state, but external clients can still try and
            # we just ignore them.
            if not self._force_volume:
                self._volume = volume
            self._seq += 1
        return self.snapshot()

    def set_force_volume(self, enabled: bool) -> PlayerSnapshot:
        """Toggle Force Volume. Turning it on pins _volume to 100
        immediately so any subsequent playback is at full scale."""
        with self._lock:
            self._force_volume = bool(enabled)
            if self._force_volume:
                self._volume = 100
            self._seq += 1
        return self.snapshot()

    def set_muted(self, muted: bool) -> PlayerSnapshot:
        with self._lock:
            self._muted = bool(muted)
            self._seq += 1
        return self.snapshot()

    def get_current_source_urls(self) -> Optional[list[str]]:
        """Return the active track's segment URLs, or None if no
        track is loaded or the source is a local file.

        Used by the DLNA manager to start FLAC passthrough when a
        session is established after a track is already playing."""
        with self._lock:
            return self._source_urls

    def get_current_track_metadata(self) -> Optional[dict]:
        """Return the active track's metadata dict, or None."""
        with self._lock:
            return self._current_track_meta

    def set_external_output_active(self, active: bool) -> None:
        """Toggle local-output silencing.

        Called by the Cast / Tidal Connect managers when
        a remote output session opens or closes. While true, the
        audio callback writes silence to the local sounddevice
        OutputStream so the user doesn't hear duplicate audio from
        Mac speakers and the remote device. Source PCM still
        flows to the remote tap before silencing, so the receiver
        gets full-amplitude audio at its own volume control.

        Idempotent. Doesn't change `_volume` or `_muted` — the
        user's manual mute / volume settings are preserved across
        a cast cycle so they don't have to re-set them when audio
        returns to local.
        """
        with self._lock:
            self._external_output_active = bool(active)
            self._seq += 1

    # --- EQ --------------------------------------------------------
    #
    # Per-track coefficients depend on sample_rate, so the
    # Equalizer instance is built / rebuilt in load() /
    # _adopt_preload_locked / _bridge_to_preload, but the user's
    # bands + preamp are remembered on the player and re-applied
    # each rebuild.

    def apply_equalizer(
        self, bands: list, preamp: Optional[float] = None
    ) -> None:
        """Apply a manual parametric band list to the active EQ.

        `bands` is a list of `ParametricBand` (or band dicts, which
        are validated/coerced here). Flat and disabled bands compile
        to no biquads; `set_sos` handles the empty-cascade cases —
        full bypass when the preamp is also unity, a preamp-only
        stage otherwise (the user's preamp must stay audible even
        when every band is flat).

        Remembered so that a stream reopen (cross-rate bridge, track
        load) rebuilds the EQ coefficients against the new sample
        rate without losing the user's curve.

        Calling this also clears any active AutoEQ profile — manual
        and profile modes are mutually exclusive in the player.
        """
        coerced = [
            b if isinstance(b, ParametricBand) else parametric_band_from_dict(b)
            for b in bands
        ]
        with self._lock:
            self._eq_parametric_bands = coerced
            self._eq_preamp = preamp
            self._eq_profile = None
            if self._eq is not None:
                sos = build_parametric_sos(coerced, self._eq.sample_rate())
                self._eq.set_sos(sos, preamp_db=preamp)

    def set_equalizer_bypass(self, bypassed: bool) -> None:
        """Toggle the A/B bypass flag without touching the active
        EQ configuration. The audio callback reads this each
        frame, so the effect is on the next callback (~10 ms).
        Used by the Phase 4 player-UI button + keyboard shortcut
        for blink-test comparison."""
        with self._lock:
            self._eq_bypass = bool(bypassed)

    def set_crossfeed_amount(self, amount_pct: int) -> None:
        """Set the Bauer crossfeed strength (0-100 percent). 0
        disables the stage entirely; non-zero values install /
        update the low-pass that bleeds bass between channels.
        Surface for the Settings page slider."""
        clamped = max(0, min(100, int(amount_pct)))
        with self._lock:
            self._crossfeed_amount = clamped
            if self._crossfeed is not None:
                if clamped > 0:
                    try:
                        self._crossfeed.set_amount(clamped)
                    except Exception:
                        log.exception("crossfeed coefficient build failed")
                        self._crossfeed.clear()
                else:
                    self._crossfeed.clear()

    def crossfeed_amount(self) -> int:
        """Current crossfeed setting in percent. 0 means bypassed."""
        with self._lock:
            return self._crossfeed_amount

    def set_crossfade(self, seconds) -> None:
        """Set the automatic-advance crossfade duration in seconds
        (clamped 0-12; 0 = off). Stored here; the fade itself is driven
        from the audio callback when a track nears its end and a
        compatible, sufficiently-buffered preload is ready. Surface for
        the Settings page slider."""
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            s = 0.0
        s = max(0.0, min(12.0, s))
        with self._lock:
            self._crossfade_s = s

    def crossfade_duration(self) -> float:
        """Current crossfade duration in seconds. 0 means disabled."""
        with self._lock:
            return self._crossfade_s

    def set_replaygain(
        self,
        mode: str,
        preamp_db: float,
        prevent_clipping: bool,
    ) -> None:
        """Configure ReplayGain leveling. `mode` is "off", "track",
        or "album"; unknown modes coerce to "off". The applied gain
        is recomputed against the currently-loaded stream's tags so
        the change takes effect immediately, not on the next track.
        """
        normalized: ReplayGainMode = (
            mode if mode in REPLAYGAIN_VALID_MODES else "off"
        )  # type: ignore[assignment]
        # Single-attribute writes are GIL-atomic; we don't need the
        # player lock here. Acquiring it would be safe but creates a
        # subtle deadlock risk for swap paths that already hold it.
        self._replaygain_mode = normalized
        self._replaygain_preamp_db = float(preamp_db)
        self._replaygain_prevent_clipping = bool(prevent_clipping)
        # Re-derive gain off the active stream's tags so the change
        # is audible immediately rather than on the next track.
        self._apply_replaygain_for(self._current_stream_info)

    def replaygain_state(self) -> dict:
        """Current configuration + the resolved gain in dB applied
        right now. Surface for the Settings page so users can see
        whether the active stream actually has tags or fell back to
        flat output."""
        mode = self._replaygain_mode
        preamp = self._replaygain_preamp_db
        prevent = self._replaygain_prevent_clipping
        tags = _replaygain_tags_from(self._current_stream_info)
        applied_db = compute_replaygain_db(tags, mode, preamp, prevent)
        return {
            "mode": mode,
            "preamp_db": preamp,
            "prevent_clipping": prevent,
            "applied_db": applied_db,
            "track_gain_db": tags.track_gain_db,
            "track_peak": tags.track_peak,
            "album_gain_db": tags.album_gain_db,
            "album_peak": tags.album_peak,
        }

    def _apply_replaygain_for(self, info: Optional[StreamInfo]) -> None:
        """Install the gain that corresponds to `info`'s tags + the
        current user mode/preamp/clipping settings. Lock-free —
        callers can be in audio-callback / lock-holding contexts
        without re-entrancy concerns. The ReplayGain instance has
        its own internal lock for the actual coefficient swap."""
        if info is None:
            self._replaygain.clear()
            return
        tags = _replaygain_tags_from(info)
        gain_db = compute_replaygain_db(
            tags,
            self._replaygain_mode,
            self._replaygain_preamp_db,
            self._replaygain_prevent_clipping,
        )
        self._replaygain.set_gain_db(gain_db)

    def equalizer_bypass(self) -> bool:
        """Read the current bypass flag — server uses it to expose
        the value via /api/eq/state."""
        with self._lock:
            return self._eq_bypass

    def apply_equalizer_profile(self, profile) -> None:
        """Switch to AutoEQ profile mode and apply `profile`. The
        profile object's bands compile to an SOS at the player's
        current sample rate; on a stream reopen the same profile
        is recompiled at the new rate (no loss across cross-rate
        bridges). Clears any manual-mode bands — the modes are
        mutually exclusive.

        Phase 5: if a user-tilt is active, the cascade includes
        the tilt shelves + preamp offset. Picking a new profile
        keeps the tilt setting (taste preference travels with
        the user, not the headphone)."""
        from app.audio.autoeq.apply import (
            TiltConfig,
            cascade_with_tilt,
        )

        with self._lock:
            self._eq_parametric_bands = []
            self._eq_preamp = None
            self._eq_profile = profile
            tilt = self._eq_tilt or TiltConfig()
            if self._eq is not None:
                sos, preamp_db = cascade_with_tilt(
                    profile, self._eq.sample_rate(), tilt
                )
                if sos.size == 0:
                    self._eq.clear()
                else:
                    self._eq.set_sos(sos, preamp_db=preamp_db)

    def apply_equalizer_tilt(self, tilt) -> None:
        """Update the user-tilt and rebuild the active cascade if
        a profile is loaded. No-op when no profile is active —
        tilt is a stack-on-top thing, not a standalone EQ.

        `tilt` is `TiltConfig | None`; passing None clears tilt
        back to flat. The profile + sample rate stay the same;
        only the trailing tilt shelves + preamp offset change."""
        from app.audio.autoeq.apply import (
            TiltConfig,
            cascade_with_tilt,
        )

        with self._lock:
            self._eq_tilt = tilt
            effective = tilt if tilt is not None else TiltConfig()
            if self._eq_profile is None or self._eq is None:
                return
            sos, preamp_db = cascade_with_tilt(
                self._eq_profile, self._eq.sample_rate(), effective
            )
            if sos.size == 0:
                self._eq.clear()
            else:
                self._eq.set_sos(sos, preamp_db=preamp_db)

    def apply_equalizer_preset(self, preset_index: int) -> list[dict]:
        """Apply a preset by index, push its curve to the live EQ,
        and return the resolved parametric bands (as dicts) so the
        frontend can snap its editor to the preset."""
        bands = parametric_preset(preset_index)
        self.apply_equalizer(bands, preamp=None)
        return [b.to_dict() for b in bands]

    # --- Device selection -------------------------------------------

    def output_stream_state(self) -> dict:
        """Snapshot of the open OutputStream — what's actually being
        fed to the OS audio API right now. Surface for the Signal
        Path readout so audiophiles can see the realised output
        format alongside the source format. All fields are None
        when the stream is closed (idle player, between tracks).

        `sd_dtype` is one of "int16" / "int32" / "float32" matching
        sounddevice's reported sample type. We translate it to a
        nominal bit depth for the UI; the underlying PortAudio path
        may pack 24-bit samples into int32 containers, which the
        readout shows as "32-bit (int32)" to be honest about it.
        """
        with self._lock:
            stream = self._stream
            sample_rate = self._stream_sample_rate
            channels = self._stream_channels
            sd_dtype = self._stream_sd_dtype
            device_id = self._selected_device_id
            external = self._external_output_active
        device_name: Optional[str] = None
        if device_id:
            for entry in list_output_devices(stream is not None):
                if entry.get("id") == device_id:
                    device_name = entry.get("name")
                    break
        return {
            "stream_open": stream is not None,
            "sample_rate_hz": sample_rate,
            "channels": channels,
            "sd_dtype": sd_dtype,
            "device_id": device_id or None,
            "device_name": device_name,
            "external_output_active": external,
        }

    def list_output_devices(self) -> list[dict]:
        """Enumerate output-capable audio devices for the picker UI.

        Thin wrapper around `output_devices.list_output_devices`:
        captures `stream_active` under the lock so the listing
        function knows whether it's safe to re-init PortAudio
        (which would tear down a live stream).

        See `app/audio/output_devices.py` for per-platform filter
        rationale and the picker payload shape.
        """
        with self._lock:
            stream_active = self._stream is not None
        return list_output_devices(stream_active)

    def set_output_device(self, device_id: str) -> None:
        """Remember the device-id selection and, if a stream is
        currently open, reopen it on the new device without
        dropping the current decoder/queue. Expected ~50ms of
        silence during the reopen — small blip, same as any
        device-switch in any audio app.
        """
        device_id = device_id or ""
        with self._pipeline_lock:
            with self._lock:
                prev_id = self._selected_device_id
                self._selected_device_id = device_id
                current_stream = self._stream
                sample_rate = self._stream_sample_rate
                channels = self._stream_channels
                sd_dtype = self._stream_sd_dtype
                was_playing = (
                    self._state == "playing"
                    and current_stream is not None
                )
            if (
                not was_playing
                or prev_id == device_id
                or sample_rate is None
                or channels is None
                or sd_dtype is None
            ):
                # Either nothing's playing (picked-up on next load),
                # the device didn't actually change, or we're missing
                # the info we'd need to reopen. Nothing more to do.
                return

            # Close the current stream + open fresh on the new
            # device. The decoder thread keeps filling the PCM
            # queue in the background, so the new stream picks up
            # where the old one left off after its first callback.
            #
            # `_replacing_stream` muzzles the finished_callback that
            # `stream.stop()` triggers — without it, the callback
            # would interpret the stop as a natural end-of-track and
            # transition state to "ended" mid-swap, briefly telling
            # the frontend playback finished.
            with self._lock:
                self._replacing_stream = True
            try:
                try:
                    current_stream.stop()
                except Exception:
                    pass
                try:
                    current_stream.close()
                except Exception:
                    pass
                with self._lock:
                    self._stream = None
                    # Reset the callback's mid-frame carry so the
                    # first post-reopen callback starts on a fresh
                    # chunk boundary — otherwise we could re-emit a
                    # partial chunk of already-played samples.
                    self._callback_carry = None
                    self._open_output_stream(sample_rate, channels, sd_dtype)
                try:
                    self._stream.start()
                except Exception:
                    log.exception("set_output_device: stream start failed")
            finally:
                with self._lock:
                    self._replacing_stream = False

    def set_exclusive_mode(self, enabled: bool) -> None:
        """Flip exclusive-mode on the audio output stream. If a stream
        is open the reopen applies immediately (~50 ms of silence,
        same as a device switch); otherwise the flag kicks in on the
        next track load."""
        enabled = bool(enabled)
        with self._pipeline_lock:
            with self._lock:
                prev = self._exclusive_mode
                self._exclusive_mode = enabled
                current_stream = self._stream
                sample_rate = self._stream_sample_rate
                channels = self._stream_channels
                sd_dtype = self._stream_sd_dtype
                was_playing = (
                    self._state == "playing" and current_stream is not None
                )
            if (
                prev == enabled
                or not was_playing
                or sample_rate is None
                or channels is None
                or sd_dtype is None
            ):
                return

            # Same intentional-stop muzzle as set_output_device — see
            # the comment there.
            with self._lock:
                self._replacing_stream = True
            try:
                try:
                    current_stream.stop()
                except Exception:
                    pass
                try:
                    current_stream.close()
                except Exception:
                    pass
                with self._lock:
                    self._stream = None
                    self._callback_carry = None
                    self._open_output_stream(sample_rate, channels, sd_dtype)
                try:
                    self._stream.start()
                except Exception:
                    log.exception("set_exclusive_mode: stream start failed")
            finally:
                with self._lock:
                    self._replacing_stream = False

    def snapshot(self) -> PlayerSnapshot:
        with self._lock:
            pos_ms = 0
            if self._stream_sample_rate and self._samples_emitted > 0:
                pos_ms = int(
                    self._samples_emitted * 1000 / self._stream_sample_rate
                )
                # Clamp to duration so the UI's progress bar doesn't
                # overshoot when the callback reports samples beyond
                # the real track end (can happen for ~one frame).
                if self._current_duration_ms > 0:
                    pos_ms = min(pos_ms, self._current_duration_ms)
            return PlayerSnapshot(
                state=self._state,
                track_id=self._current_track_id,
                position_ms=pos_ms,
                duration_ms=self._current_duration_ms,
                volume=self._volume,
                muted=self._muted,
                error=self._last_error,
                seq=self._seq,
                stream_info=self._current_stream_info,
                force_volume=self._force_volume,
            )

    # --- internals --------------------------------------------------

    @staticmethod
    def _dispose_pipeline_async(thread, decoder) -> None:
        """Join a torn-down decoder thread and close its decoder, off the
        realtime / request path (see the call site in `_teardown`).

        Ordering is load-bearing: the thread must have exited the PyAV
        container before `close()` frees it, so we join first. The join
        is bounded — a permanently wedged read can't pin this disposer
        (and it's a daemon, so it never blocks process exit either). The
        decoder being disposed here is already detached from the player
        (`self._decoder`/`self._decoder_thread` were cleared before the
        spawn), so this can't race the next track's fresh pipeline."""
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if decoder is not None:
            try:
                decoder.close()
            except Exception:
                pass

    def _teardown(self) -> None:
        """Stop the pipeline fully. State transitions to idle BEFORE
        we close the stream, so the finished_callback short-circuits
        and doesn't emit a spurious 'ended' at user-initiated stops.
        Any preload is also dropped — a stale preload on a
        user-stopped pipeline would fire a bogus bridge when the
        next load() came in.
        """
        # Drop preload first so finished_callback's `pre` read sees
        # None when it fires during stream.close() below.
        self._drop_preload()
        self._pending_upnp_notify = False
        with self._lock:
            # If we're already idle, nothing to do.
            if self._state == "idle" and self._stream is None:
                return
            self._state = "idle"
            self._seq += 1

        self._stop_flag.set()
        # Cancel the decoder's source BEFORE joining the thread.
        # The thread is very often mid-HTTP-fetch inside SegmentReader
        # when a track change fires, and stop_flag is only checked
        # between reads. Closing the source aborts the in-flight
        # request so read() raises on the thread and it exits in
        # single-digit ms instead of ~400ms.
        decoder = self._decoder
        if decoder is not None:
            try:
                decoder.cancel_source()
            except Exception:
                pass

        stream = self._stream
        self._stream = None
        if stream is not None:
            # abort() over stop(): stop() drains any buffered audio
            # to the device before returning, which on track-change
            # costs 400-900ms of nothing-is-happening. abort()
            # discards the pending buffer immediately and we start
            # the next track faster. Any in-flight tail audio just
            # gets cut off, which is what the user already expects
            # when clicking a new track.
            t_abort0 = time.monotonic()
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            print(
                f"[perf] teardown stream.abort+close="
                f"{(time.monotonic() - t_abort0) * 1000.0:.0f}ms",
                file=sys.stderr,
                flush=True,
            )

        # The stream is aborted, so the audio callback can no longer fire —
        # safe now to cancel any in-progress crossfade and dispose the
        # incoming preload we'd claimed (a manual skip / stop landing inside
        # the fade window). No-op when no crossfade is active.
        self._abort_crossfade()

        # Dispose the old decoder thread + decoder OFF this path. The
        # join blocks until the thread unwinds its wedged Tidal segment
        # read — ~1s on a contended / slow network even though
        # cancel_source() above already aborted the in-flight request —
        # and _teardown runs on the request thread holding the GIL, so a
        # synchronous join here starves the realtime audio callback for
        # that whole second: an audible ~1s dropout at every track change
        # on a loaded machine (visible as the back-to-back
        # "[perf] teardown thread.join=~1300ms" + "callback jitter late
        # by ~770ms" pair in audio.log). Hand the join + ordered close
        # (close must follow the thread leaving the PyAV container) to a
        # daemon so the next track loads immediately; the old pipeline
        # finishes unwinding in the background.
        thread = self._decoder_thread
        self._decoder_thread = None
        self._decoder = None
        if thread is not None or decoder is not None:
            threading.Thread(
                target=self._dispose_pipeline_async,
                args=(thread, decoder),
                daemon=True,
                name="pcm-dispose",
            ).start()

        with self._lock:
            self._drain_queue_locked()
            self._callback_carry = None
            self._samples_emitted = 0
            self._current_track_id = None
            self._current_duration_ms = 0
            self._current_stream_info = None
            self._stream_sample_rate = None
            self._source_urls = None
            self._source_path = None
            self._current_track_meta = None
            self._paused = False
            self._seeking = False

    def _resolve_source(
        self, track_id: str, quality: Optional[str]
    ) -> tuple[Union[str, list[str]], Optional[float], StreamInfo, dict[int, bytes]]:
        """Resolve a track id to a source spec.

        Returns either a local filesystem path (str) or a DASH URL
        list (list[str] — index 0 is the init segment). The caller
        constructs a `Decoder` from it via `_build_source()` and
        stores the spec on the player so seek can rebuild.

        The fourth return value is a map of pre-fetched segment
        bytes (idx -> bytes). Empty for local files and for stream
        sources that have not had byte-level prefetch run against
        them. When populated, SegmentReader seeds its buffer with
        those bytes so decoder_init does not touch the network.
        """
        if self._local_lookup is not None:
            local_path = self._local_lookup(track_id)
            if local_path and os.path.exists(local_path):
                info, duration_s = _probe_local_stream_info(local_path)
                return (
                    local_path,
                    duration_s,
                    info or StreamInfo(source="local"),
                    {},
                )

        session = self._session_getter()
        if quality and self._quality_clamp is not None:
            try:
                clamped = self._quality_clamp(quality)
                if clamped and clamped != quality:
                    log.info(
                        "clamped streaming quality %r -> %r", quality, clamped
                    )
                    quality = clamped
            except Exception:
                pass

        # Cache hit path: skip the two parallel Tidal round-trips
        # (track metadata + playbackinfo) plus the local manifest
        # parse when a recent prefetch or play already resolved this
        # (track_id, quality) pair. 3-minute TTL stays well inside
        # Tidal's signed-URL expiry window.
        cache_key = (track_id, quality)
        t0 = time.monotonic()
        cached_urls_info = self._manifest_cache.lookup(cache_key)
        if cached_urls_info is not None:
            urls, duration, info, bytes_map = cached_urls_info
            print(
                f"[perf] resolve track={track_id} quality={quality} "
                f"cache=HIT elapsed={(time.monotonic() - t0) * 1000.0:.0f}ms "
                f"prefetched_segments={len(bytes_map)}",
                file=sys.stderr,
                flush=True,
            )
            return (list(urls), duration, info, bytes_map)

        try:
            return self._resolve_uncached(
                session, track_id, quality, cache_key, t0
            )
        except Exception as exc:
            # The playback resolve path must recover from an expired
            # Tidal token exactly like the download and metadata
            # paths already do. Without this, a stale access token
            # surfaces as a silent "press play, nothing happens":
            # the 401 propagates, the player flips to error, and the
            # user is neither refreshed nor bounced to Login.
            # getattr (not self._force_refresh) because some unit
            # tests build PCMPlayer via __new__ and never run
            # __init__; a missing injector just means "no refresh".
            force_refresh = getattr(self, "_force_refresh", None)
            if force_refresh is not None and _looks_like_auth_error(exc):
                if force_refresh():
                    log.info(
                        "playback resolve hit a Tidal auth error; "
                        "token refreshed, retrying once"
                    )
                    return self._resolve_uncached(
                        self._session_getter(),
                        track_id,
                        quality,
                        cache_key,
                        t0,
                    )
                raise RuntimeError(
                    "Tidal session expired. Please log out and log "
                    "back in."
                ) from exc
            raise

    def _resolve_uncached(
        self,
        session: tidalapi.Session,
        track_id: str,
        quality: Optional[str],
        cache_key: tuple,
        t0: float,
    ) -> tuple[
        Union[str, list[str]], Optional[float], StreamInfo, dict[int, bytes]
    ]:
        """Cache-miss network resolution: fetch track + playback-info,
        parse the manifest, populate the cache. Split out of
        _resolve_source so the caller can retry it once after a
        force-refresh when Tidal returns an expired-token 401."""
        override = None
        if quality:
            try:
                override = tidalapi.Quality[quality]
            except KeyError:
                log.warning("unknown quality %r, using session default", quality)
        original = session.config.quality
        if override is not None:
            session.config.quality = override
        try:
            # The metadata fetch (`session.track`) and the playback-info
            # fetch (`track.get_stream`) are two independent Tidal
            # round-trips: get_stream only uses the track id, not any
            # parsed metadata. Run them in parallel so total wall-clock
            # is max() instead of sum() — saves ~50-150 ms per cache
            # miss on a click. The third historical "phase",
            # `stream.get_stream_manifest()`, is a local base64 +
            # parse, not a network call.
            tid = int(track_id)
            t_start = time.monotonic()
            # Not a `with` block: ThreadPoolExecutor.__exit__ does
            # shutdown(wait=True), which would re-block on a hung
            # future even after result(timeout=) gave up — defeating
            # the whole point of bounding the wait. shutdown(wait=
            # False, cancel_futures=True) lets us return promptly; the
            # transport timeout (app.http.DEFAULT_TIMEOUT) ensures the
            # orphaned worker actually unwinds shortly after instead
            # of lingering.
            ex = ThreadPoolExecutor(max_workers=2)
            try:
                track_future = ex.submit(session.track, tid)
                stream_holder = tidalapi.Track(session)
                stream_holder.id = tid
                stream_future = ex.submit(stream_holder.get_stream)
                try:
                    track = track_future.result(timeout=_RESOLVE_TIMEOUT_S)
                    stream = stream_future.result(timeout=_RESOLVE_TIMEOUT_S)
                except FutureTimeoutError:
                    # Surfaces to _load_locked's except: it flips the
                    # player to state=error and emits, so the frontend
                    # clears the spinner and the user can pick another
                    # track — instead of a permanent "loading" that
                    # only an app restart escaped.
                    raise RuntimeError(
                        "Tidal did not respond while resolving the "
                        "track (timed out)"
                    )
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
            t_after_network = time.monotonic()
            manifest = stream.get_stream_manifest()
            t_end = time.monotonic()
            if getattr(manifest, "is_encrypted", False):
                raise RuntimeError("encrypted stream — can't decode")
            urls = list(getattr(manifest, "urls", []) or [])
            if not urls:
                raise RuntimeError("manifest has no segment urls")
            duration = getattr(track, "duration", None)
            # Save track metadata for UPnP/DLNA track-change notification.
            artist_name = (
                getattr(track.artist, "name", None)
                if hasattr(track, "artist")
                else None
            )
            if not artist_name and hasattr(track, "artists") and track.artists:
                artist_name = track.artists[0].name
            album_name = (
                track.album.name
                if hasattr(track, "album") and track.album
                else None
            )
            cover_id = (
                track.album.cover
                if hasattr(track, "album") and track.album
                else None
            )
            self._current_track_meta = {
                "title": getattr(track, "name", "") or "",
                "artist": artist_name or "",
                "album": album_name or "",
                "duration_s": duration or 0,
                "cover_url": (
                    f"https://resources.tidal.com/images/{cover_id}/640x640.jpg"
                    if cover_id
                    else ""
                ),
            }
            info = StreamInfo(
                source="stream",
                codec=_normalize_codec(
                    getattr(manifest, "codecs", None)
                    or getattr(manifest, "get_codecs", lambda: None)()
                ),
                bit_depth=_safe_int(getattr(stream, "bit_depth", None)),
                sample_rate_hz=_safe_int(getattr(stream, "sample_rate", None)),
                audio_quality=getattr(stream, "audio_quality", None),
                audio_mode=getattr(stream, "audio_mode", None),
                # tidalapi exposes the EBU R128 ReplayGain values + actual
                # peaks straight off the Stream object. Storing them on
                # StreamInfo means the audio engine can read them through
                # the same channel everything else flows through (cache
                # entry, preload, current track) without a separate
                # plumbing path.
                track_replay_gain_db=_safe_float(
                    getattr(stream, "track_replay_gain", None)
                ),
                track_peak=_safe_float(
                    getattr(stream, "track_peak_amplitude", None)
                ),
                album_replay_gain_db=_safe_float(
                    getattr(stream, "album_replay_gain", None)
                ),
                album_peak=_safe_float(
                    getattr(stream, "album_peak_amplitude", None)
                ),
            )
            duration_s = float(duration) if duration else None
            self._manifest_cache.store(cache_key, list(urls), duration_s, info)
            # After store, any previously-warmed bytes are preserved
            # — pick them back up so a MISS on manifest that still
            # has bytes cached returns the bytes too.
            cached = self._manifest_cache.lookup(cache_key)
            bytes_map = cached[3] if cached is not None else {}
            print(
                f"[perf] resolve track={track_id} quality={quality} cache=MISS "
                f"total={(t_end - t0) * 1000.0:.0f}ms "
                f"network_parallel={(t_after_network - t_start) * 1000.0:.0f}ms "
                f"manifest_parse={(t_end - t_after_network) * 1000.0:.0f}ms "
                f"segments={len(urls)}",
                file=sys.stderr,
                flush=True,
            )
            return (list(urls), duration_s, info, bytes_map)
        finally:
            if override is not None:
                session.config.quality = original

    def cache_stats(self) -> dict:
        """Snapshot of the manifest cache for the /api/player/cache-stats
        endpoint. Read while testing the prefetch path to confirm
        hovers / album-mount prefetches are landing."""
        return self._manifest_cache.stats()

    def prefetch(
        self, track_id: str, quality: Optional[str] = None, *, warm_bytes: bool = True
    ) -> bool:
        """Populate the manifest cache for a track without starting
        playback. Safe to call speculatively from hover / album-mount
        warmers. Returns True if the cache now has an entry for this
        (track_id, quality), False on any failure — callers are
        fire-and-forget so errors are swallowed.

        When warm_bytes=True (default), follows up by downloading the
        init segment plus the first media segment in parallel and
        caches those bytes too, so the next click's decoder_init
        can skip the network entirely.

        Jitters 50-200ms before the first Tidal request. Album-mount
        prefetch fans out via a ThreadPoolExecutor: without jitter, all
        N workers hit `_resolve_source` within the same few ms, and
        each one then issues two parallel Tidal API calls (track
        metadata + playbackinfo). That stack of N*2 requests inside
        one second is exactly the burst pattern Tidal's anti-abuse
        layer flags as a 429 (with longer 403/`abuse_detected`
        escalations from there). The jitter is per-worker, so the
        first request from each worker spreads across a 150 ms window
        before the parallel pair even starts. Foreground play goes
        through the same `_resolve_source` path so it benefits from
        the parallelization too, but doesn't go through this jitter
        wrapper since there's only one foreground request at a time.

        No-ops when prefetch is disabled by the caller — the endpoint
        layer bails out on offline_mode before ever reaching this
        method, so we only get here when prefetch is wanted."""
        # Lazy import: tidal_client doesn't import player, but
        # importing it from module-load time would still couple the
        # two trees together for no reason. The function is a tiny
        # `time.sleep(uniform(...))`, so the import cost is paid once
        # per process at first prefetch and never again.
        from app.tidal_client import tidal_jitter_sleep

        tidal_jitter_sleep()
        try:
            source_spec, _dur, _info, _bytes = self._resolve_source(track_id, quality)
        except Exception as exc:
            log.debug("prefetch manifest %s@%s failed: %s", track_id, quality, exc)
            return False
        if warm_bytes and isinstance(source_spec, list):
            self._warm_bytes((track_id, quality), source_spec)
        return True

    def _warm_bytes(
        self, key: tuple[str, Optional[str]], urls: list[str]
    ) -> None:
        """Best-effort download of the init + first media segments
        in parallel. Skips segments already in the cache so repeat
        hover events don't re-download. Fire-and-forget: any failure
        leaves the cache at URL-only and the eventual play path
        falls through to the normal SegmentReader fetch."""
        cached = self._manifest_cache.lookup(key)
        have = cached[3] if cached is not None else {}
        targets = [i for i in (0, 1) if i < len(urls) and i not in have]
        if not targets:
            return
        fetched: dict[int, bytes] = {}

        def _fetch(i: int) -> Optional[tuple[int, bytes]]:
            try:
                r = requests.get(urls[i], timeout=30)
                r.raise_for_status()
                return i, r.content
            except Exception as exc:
                log.debug("prefetch bytes seg=%d failed: %s", i, exc)
                return None

        with ThreadPoolExecutor(
            max_workers=min(2, len(targets)),
            thread_name_prefix="warm-bytes",
        ) as pool:
            for result in pool.map(_fetch, targets):
                if result is not None:
                    idx, content = result
                    fetched[idx] = content
        if fetched:
            total = sum(len(v) for v in fetched.values())
            self._manifest_cache.update_bytes(key, fetched)
            print(
                f"[perf] prefetch bytes track={key[0]} quality={key[1]} "
                f"segments={sorted(fetched.keys())} "
                f"total={total}B",
                file=sys.stderr,
                flush=True,
            )

    def _open_output_stream(self, sample_rate: int, channels: int, dtype: str) -> None:
        # If the user picked a specific output device, resolve its
        # saved name to a live PortAudio index; otherwise device=None
        # routes to the system default. When the chosen device is no
        # longer present as an output (unplugged, or a legacy numeric
        # id from before device identity was name-based), fall back to
        # the default instead of hard-failing the load. Issue #245 was
        # a drifted index landing on an input-only device, where
        # sd.OutputStream raised "Invalid number of channels".
        device, available = resolve_output_device(self._selected_device_id)
        if self._selected_device_id and not available:
            print(
                f"[audio] saved output device "
                f"{self._selected_device_id!r} is not available; "
                "falling back to system default",
                flush=True,
            )

        # Decide the rate we'll actually feed sounddevice.
        # ─ Exclusive Mode: push the source rate straight at the device.
        #   The OS or driver is asked to reconfigure to that rate, and
        #   either succeeds (bit-perfect) or fails (we fall back below).
        # ─ Shared mode: ask the device for its mixer rate and resample
        #   to that rate inside the decoder. The OS then receives audio
        #   that already matches its mixer's rate, so its own resampler
        #   is a no-op and the only resampler in the chain is ours,
        #   where we control the headroom. When the source rate already
        #   matches the device's mixer rate (e.g. 44.1 source on a 44.1
        #   device), set_target_rate is a no-op and we stay bit-perfect
        #   even in shared mode.
        decoder = self._decoder
        if decoder is not None:
            if self._exclusive_mode:
                target_rate = decoder.sample_rate
            else:
                target_rate = self._query_device_mixer_rate(device, decoder.sample_rate)
            decoder.set_target_rate(target_rate)
            sample_rate = decoder.output_sample_rate
            dtype = decoder.sounddevice_dtype
            channels = decoder.channels

        # Exclusive Mode — push PCM straight at the device at its
        # native rate / bit depth. On macOS the CoreAudio flags ask
        # the driver to reconfigure the device and fail loudly
        # instead of silently resampling. On Windows we open WASAPI
        # exclusive. On other platforms the flag is a no-op.
        extra_settings = None
        if self._exclusive_mode:
            try:
                if sys.platform == "darwin":
                    extra_settings = sd.CoreAudioSettings(
                        change_device_parameters=True,
                        fail_if_conversion_required=True,
                    )
                elif sys.platform.startswith("win"):
                    extra_settings = sd.WasapiSettings(exclusive=True)
            except AttributeError:
                # Older sounddevice / PortAudio without these helpers.
                # Fall through to the shared-mode stream so the user
                # still gets audio, just without the exclusive guarantee.
                extra_settings = None

        stream_kwargs: dict = dict(
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
            device=device,
            callback=self._audio_callback,
            finished_callback=self._on_stream_finished,
            # PortAudio defaults to its lowest-latency setting (~5-10ms
            # on macOS, similar on Windows WASAPI), which leaves the
            # callback no headroom when a concurrent thread holds the
            # GIL for longer than the buffer can drain. Heavy Python
            # work elsewhere in the app — building a few hundred dicts
            # for the artist endpoint, parsing a 200KB Spotify GraphQL
            # response, occasional gen-0 GC sweeps — can hold the GIL
            # for 5-15ms, and the audio callback (which has to acquire
            # the GIL to enter Python) misses its deadline. The user
            # hears that as stutter.
            #
            # The floor (100ms) gives the callback ~10x more headroom
            # than PortAudio's default; the user-perceived cost of 100ms
            # on play / seek / pause is imperceptible for music. But a
            # fixed 100ms starves a Bluetooth device, whose A2DP pipeline
            # buffers far deeper — that was a continuous-crackle bug on BT
            # output. _output_latency_for honours the device's own deeper
            # latency when it has one, so BT gets the buffer it needs.
            latency=self._output_latency_for(device),
        )
        if extra_settings is not None:
            stream_kwargs["extra_settings"] = extra_settings

        try:
            self._stream = sd.OutputStream(**stream_kwargs)
        except Exception:
            # Exclusive Mode can fail on devices that refuse the
            # requested rate / format (e.g. a USB DAC pinned to 48k
            # when the track is 44.1k). Fall back to shared mode so
            # playback keeps working. We also need to switch the
            # decoder to mixer-rate output for the same intersample-
            # peak reason as above, since the fallback puts us in
            # shared mode.
            if extra_settings is not None:
                log.warning(
                    "Exclusive-mode stream open failed; falling back to shared"
                )
                stream_kwargs.pop("extra_settings", None)
                if decoder is not None:
                    fallback_rate = self._query_device_mixer_rate(
                        device, decoder.sample_rate
                    )
                    decoder.set_target_rate(fallback_rate)
                    stream_kwargs["samplerate"] = decoder.output_sample_rate
                    stream_kwargs["dtype"] = decoder.sounddevice_dtype
                    sample_rate = decoder.output_sample_rate
                    channels = decoder.channels
                self._stream = sd.OutputStream(**stream_kwargs)
            else:
                raise

        # Pin the cached stream-state to whatever we actually opened
        # at. set_exclusive_mode and set_output_device read these to
        # decide gapless compatibility, so the values must reflect
        # the post-reconfig rate, not the source rate. Doing this
        # here (vs. at every callsite) keeps the rule in one place.
        self._stream_sample_rate = sample_rate
        self._stream_sd_dtype = dtype
        self._stream_channels = channels

        # Diagnostic line for the audio open. Shows the exact rate /
        # depth / channel count / host API / exclusive flag we ended
        # up using. Critical for triaging Windows-side audio bugs
        # where a stutter or freeze typically traces back to a rate
        # mismatch between the decoder and the device, or to
        # exclusive-mode being on for a device that only supports a
        # subset of the rates we're feeding. Printed unconditionally
        # so it lands in the user's console log without needing a
        # debug flag — the volume is one line per stream open.
        try:
            dev_info = sd.query_devices(device, kind="output")
            ha = sd.query_hostapis(dev_info.get("hostapi"))
            ha_name = ha.get("name") if isinstance(ha, dict) else "?"
        except Exception:
            ha_name = "?"
        print(
            f"[audio] stream open: device={device} rate={sample_rate}Hz "
            f"channels={channels} dtype={dtype!r} hostapi={ha_name!r} "
            f"exclusive={self._exclusive_mode}",
            file=sys.stderr,
            flush=True,
        )

        # Rebuild the EQ against the new sample rate. Preserves
        # whichever mode is active — manual bands or AutoEQ profile.
        self._eq = Equalizer(sample_rate=sample_rate, channels=channels)
        if self._eq_profile is not None:
            try:
                from app.audio.autoeq.apply import (
                    TiltConfig,
                    cascade_with_tilt,
                )

                tilt = self._eq_tilt or TiltConfig()
                sos, preamp_db = cascade_with_tilt(
                    self._eq_profile, sample_rate, tilt
                )
                if sos.size == 0:
                    self._eq.clear()
                else:
                    self._eq.set_sos(sos, preamp_db=preamp_db)
            except Exception:
                log.exception("autoeq profile coefficient build failed")
                self._eq.clear()
        elif self._eq_parametric_bands or self._eq_preamp is not None:
            # `or preamp` — a preamp-only curve (no bands) must
            # survive the reopen too; set_sos installs the
            # preamp-only stage for an empty cascade.
            try:
                sos = build_parametric_sos(
                    self._eq_parametric_bands, sample_rate
                )
                self._eq.set_sos(sos, preamp_db=self._eq_preamp)
            except Exception:
                log.exception("eq coefficient build failed")
                self._eq.clear()

        # Crossfeed coefficients depend on the active sample rate, so
        # rebuild it here too. The user setting (`_crossfeed_amount`)
        # survives the rebuild — same pattern as EQ.
        self._crossfeed = Crossfeed(sample_rate=sample_rate)
        if self._crossfeed_amount > 0:
            try:
                self._crossfeed.set_amount(self._crossfeed_amount)
            except Exception:
                log.exception("crossfeed coefficient build failed")
                self._crossfeed.clear()

    @staticmethod
    def _query_device_mixer_rate(device: Optional[int], fallback: int) -> int:
        """Look up the rate the OS will mix at for `device` in shared
        mode. If sounddevice can't tell us, return `fallback` so the
        decoder stays at source rate (the previous behavior). Rounded
        to int because PortAudio reports it as a float."""
        try:
            info = sd.query_devices(device, kind="output")
        except Exception:
            log.warning("query_devices failed; using source rate as fallback")
            return fallback
        rate = info.get("default_samplerate") if isinstance(info, dict) else None
        if not rate or rate <= 0:
            return fallback
        return int(round(rate))

    # Floor on the requested output latency. This is the headroom the
    # realtime callback needs to ride out GIL/CPU contention on wired,
    # low-latency devices — see the long note in _open_output_stream.
    _OUTPUT_LATENCY_FLOOR_S = 0.1

    @staticmethod
    def _output_latency_for(device: Optional[int]) -> float:
        """Suggested PortAudio output latency for `device`.

        The floor (_OUTPUT_LATENCY_FLOOR_S) is what wired/low-latency
        outputs need for GIL headroom. Bluetooth is different: its A2DP
        pipeline buffers deeply (~150-300 ms), so asking PortAudio for
        only the floor starves the device and the user hears continuous
        crackle on BT output. A device's own `default_high_output_latency`
        reflects that real pipeline depth, so honour whichever is larger.
        `device=None` resolves to the default output. Best-effort: any
        query failure falls back to the floor (the previous fixed value)."""
        try:
            info = sd.query_devices(device, kind="output")
        except Exception:
            return PCMPlayer._OUTPUT_LATENCY_FLOOR_S
        high = info.get("default_high_output_latency") if isinstance(info, dict) else None
        try:
            high_f = float(high)
        except (TypeError, ValueError):
            high_f = 0.0
        return max(PCMPlayer._OUTPUT_LATENCY_FLOOR_S, high_f)

    def _start_decoder_thread(self) -> None:
        # Bind the decoder / queue / flags to the thread's locals at
        # creation time. The loop must NOT read self.* fields — a
        # concurrent load() may rebind those, which previously caused
        # two decoder threads to share one PyAV generator (hence
        # "generator already executing" errors under rapid back-to-
        # back play_track calls).
        decoder = self._decoder
        pcm_queue = self._pcm_queue
        stop_flag = self._stop_flag
        done_event = self._decoder_done
        t = threading.Thread(
            target=self._decoder_loop,
            args=(decoder, pcm_queue, stop_flag, done_event),
            name="pcm-decoder",
            daemon=True,
        )
        self._decoder_thread = t
        t.start()

    @staticmethod
    def _decoder_loop(
        decoder: Optional[Decoder],
        pcm_queue: "queue.Queue[Optional[np.ndarray]]",
        stop_flag: threading.Event,
        done_event: threading.Event,
    ) -> None:
        if decoder is None:
            done_event.set()
            return
        try:
            while not stop_flag.is_set():
                pcm = decoder.next_pcm()
                if pcm is None:
                    break
                while not stop_flag.is_set():
                    try:
                        pcm_queue.put(pcm, timeout=0.5)
                        break
                    except queue.Full:
                        continue
        except Exception:
            log.exception("decoder thread crashed")
        finally:
            done_event.set()
            # Push the sentinel onto the thread's OWN queue, not
            # whatever self._pcm_queue happens to point at now. If
            # a gapless swap rebound self._pcm_queue to a different
            # queue mid-loop, pushing the sentinel there would have
            # incorrectly flagged the new decoder as done.
            try:
                pcm_queue.put_nowait(None)
            except queue.Full:
                pass

    def audio_health(self) -> dict:
        """Cumulative playback-health counters since this player was
        constructed, for the activity report. Each count maps to a
        distinct stutter cause so a bug report can be triaged without
        the audio.log:

          output_underruns  — PortAudio flagged an over/underrun
                              (driver / exclusive-mode / device can't
                              keep up).
          queue_starvations — our decoder/network didn't fill the PCM
                              buffer in time (throughput / CPU).
          callback_jitter_events — the realtime callback was delivered
                              late while the queue was full (GIL/CPU
                              contention elsewhere in the app).

        worst_jitter_late_ms is the largest single late delivery seen.
        The queue depth/max and samples_emitted give a point-in-time
        sense of buffer fill and how much has played. All best-effort:
        the queue may not exist yet if nothing has played.
        """
        q = getattr(self, "_pcm_queue", None)
        try:
            queue_depth = q.qsize() if q is not None else None
        except Exception:
            queue_depth = None
        return {
            "output_underruns": self._cb_status_total,
            "queue_starvations": self._cb_starve_total,
            "callback_jitter_events": self._cb_jitter_total,
            "worst_jitter_late_ms": round(self._cb_jitter_worst_late_ms, 1),
            "samples_emitted": getattr(self, "_samples_emitted", 0),
            "pcm_queue_depth": queue_depth,
            "pcm_queue_max": _PCM_QUEUE_MAX,
        }

    def _log_callback_status(self, status) -> None:
        """Rate-limited diagnostic for PortAudio over/underruns
        signalled via the callback's `status` arg. One message + a
        running count per second; without rate limiting a sustained
        glitch at 86 Hz callback cadence would saturate stderr.
        Critical diagnostic on Windows where stutter complaints
        typically trace back to this path."""
        now = time.monotonic()
        self._cb_status_count += 1
        self._cb_status_total += 1
        if now - self._cb_status_last_print >= 1.0:
            count = self._cb_status_count
            self._cb_status_count = 0
            self._cb_status_last_print = now
            print(
                f"[audio] callback status={status} "
                f"(count_since_last={count})",
                file=sys.stderr,
                flush=True,
            )
            try:
                audio_log.info(
                    "callback status=%s count_since_last=%d", status, count
                )
            except Exception:
                pass

    def _log_callback_heartbeat(self) -> None:
        """DEBUG-only per-100-callback heartbeat. Confirms the
        callback is running + advancing samples and shows the
        queue depth so a "decoder is filling but callback isn't
        draining" condition is visible at a glance."""
        self._callback_counter += 1
        if self._callback_counter % 100 == 0:
            qsize = self._pcm_queue.qsize()
            print(
                f"[pcm] callback tick #{self._callback_counter} "
                f"samples_emitted={self._samples_emitted} "
                f"queue={qsize}/{_PCM_QUEUE_MAX} "
                f"state={self._state} "
                f"track={self._current_track_id}",
                file=sys.stderr, flush=True,
            )

    def _log_callback_starvation(self) -> None:
        """Rate-limited diagnostic for our-side underruns — the
        decoder didn't push a chunk in time. Distinct from
        `_log_callback_status` because it pinpoints the cause as
        ours (slow disk / network / CPU) rather than driver-side."""
        now = time.monotonic()
        self._cb_starve_count += 1
        self._cb_starve_total += 1
        if now - self._cb_starve_last_print >= 1.0:
            count = self._cb_starve_count
            self._cb_starve_count = 0
            self._cb_starve_last_print = now
            print(
                f"[audio] queue starvation: decoder behind "
                f"the callback (count_since_last={count})",
                file=sys.stderr,
                flush=True,
            )
            try:
                audio_log.info(
                    "queue starvation count_since_last=%d", count
                )
            except Exception:
                pass

    def _log_callback_jitter(self, gap_s: float, expected_s: float) -> None:
        """Rate-limited diagnostic for a missed callback deadline: the
        realtime thread was serviced `gap_s` after the previous audio
        callback, well past the `expected_s` buffer period. Distinct
        from starvation (queue had data) and from PortAudio status
        (the device didn't necessarily flag it) — this is the GIL/CPU
        contention crackle, the kind you hear when navigating pages
        mid-playback. One line + worst-case + count per second."""
        now = time.monotonic()
        self._cb_jitter_count += 1
        self._cb_jitter_total += 1
        gap_ms = gap_s * 1000.0
        if gap_ms > self._cb_jitter_worst_ms:
            self._cb_jitter_worst_ms = gap_ms
        late_ms = gap_ms - expected_s * 1000.0
        if late_ms > self._cb_jitter_worst_late_ms:
            self._cb_jitter_worst_late_ms = late_ms
        if now - self._cb_jitter_last_print >= 1.0:
            count = self._cb_jitter_count
            worst = self._cb_jitter_worst_ms
            self._cb_jitter_count = 0
            self._cb_jitter_worst_ms = 0.0
            self._cb_jitter_last_print = now
            msg = (
                f"callback jitter: late by "
                f"{gap_ms - expected_s * 1000.0:.0f}ms "
                f"(gap={gap_ms:.0f}ms expected={expected_s * 1000.0:.1f}ms "
                f"worst={worst:.0f}ms count_since_last={count}) "
                f"— missed audio deadline, queue was not starved"
            )
            print(f"[audio] {msg}", file=sys.stderr, flush=True)
            try:
                audio_log.info(msg)
            except Exception:
                pass

    def _stall_watchdog(self) -> None:
        """Detect a wedged audio callback and dump every thread's
        stack so the freeze diagnoses itself.

        The realtime callback sets _cb_last_t on every invocation,
        including while paused/seeking (it still fires to output
        silence), so a stale _cb_last_t while state is "playing"
        means the callback thread is blocked, not idle. 10s is far
        beyond any normal buffer/jitter (callbacks run ~12x/sec), so
        there are no false positives from scheduling hiccups.

        Strictly lock-free: it must be able to fire *during* a _lock
        deadlock, so it only reads plain attributes and never takes
        _lock. Fires once per stall; rearms when audio resumes.
        """
        STALL_S = 10.0
        # A load that never produces audio leaves the player pinned in
        # "loading" forever. Beyond any plausible real load time (the
        # [perf] load lines run ~1-2s even on a contended machine),
        # surface it as an error so the UI stops hanging and — with
        # "continue playing" on — auto-advances past the dead track.
        LOAD_STALL_S = 30.0
        while True:
            time.sleep(5.0)
            try:
                # Stuck-loading recovery. Tracked here (not via a hook in
                # the load path) so there's one owner of the timer.
                if self._state == "loading":
                    if self._loading_since is None:
                        self._loading_since = time.monotonic()
                    elif time.monotonic() - self._loading_since > LOAD_STALL_S:
                        self._force_load_stall_error()
                        self._loading_since = None
                else:
                    self._loading_since = None
            except Exception:
                pass
            try:
                last = self._cb_last_t
                active = (
                    self._stream is not None
                    and self._state == "playing"
                    and not self._paused
                    and not self._seeking
                    and bool(last)
                )
                if not active:
                    self._stall_dumped = False
                    continue
                silent_for = time.monotonic() - last
                if silent_for < STALL_S:
                    self._stall_dumped = False
                    continue
                if self._stall_dumped:
                    continue
                self._stall_dumped = True
                from app.paths import user_data_dir

                path = user_data_dir() / "audio_stall.txt"
                header = (
                    f"AUDIO STALL: callback silent for {silent_for:.1f}s "
                    f"state={self._state} track={self._current_track_id}"
                )
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(header + "\n\n")
                        faulthandler.dump_traceback(fh, all_threads=True)
                except Exception:
                    pass
                try:
                    audio_log.info(f"{header}; thread dump -> {path}")
                except Exception:
                    pass
                print(
                    f"[player] {header}; thread dump -> {path}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception:
                # The watchdog must never take the process down.
                pass

    def _force_load_stall_error(self) -> None:
        """Recover a load wedged in "loading" past LOAD_STALL_S by
        surfacing an error, so the UI stops hanging and (with continue-
        playing on) the frontend advances past the dead track.

        Uses a NON-blocking lock acquire to keep the watchdog's
        survive-a-deadlock invariant: if `_lock` is contended we skip
        this round rather than park the watchdog. Best-effort and never
        raises out to the caller."""
        track = self._current_track_id
        if not self._lock.acquire(blocking=False):
            return
        try:
            # Re-check under the lock — the load may have just completed.
            if self._state != "loading":
                return
            self._state = "error"
            self._last_error = "Track load timed out"
            self._seq += 1
        finally:
            self._lock.release()
        print(
            f"[pcm] load-stall watchdog: track={track} stuck in 'loading' "
            f">30s; forced error so playback can recover",
            file=sys.stderr,
            flush=True,
        )
        try:
            audio_log.info(
                "load-stall: track=%s stuck in loading; forced error", track
            )
        except Exception:
            pass
        try:
            self._emit()
        except Exception:
            pass

    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self._log_callback_status(status)
        if PCMPlayer._DEBUG:
            self._log_callback_heartbeat()

        # Callback-jitter check. Only meaningful between two
        # consecutive callbacks that both delivered continuous audio;
        # a gap after pause/seek/swap/underrun/stop is expected, not a
        # glitch, so those paths leave _cb_prev_audio False. A gap of
        # >= 2x the buffer period means the realtime thread was
        # serviced a full buffer late — that's the audible crackle.
        now = time.monotonic()
        if self._cb_prev_audio and self._cb_last_t:
            rate = self._stream_sample_rate or 0
            if rate:
                expected = frames / float(rate)
                gap = now - self._cb_last_t
                if expected > 0.0 and gap >= expected * 2.0:
                    self._log_callback_jitter(gap, expected)
        self._cb_last_t = now
        self._cb_prev_audio = False

        # Paused → output silence, don't drain queue, don't advance
        # position. Zero-latency resume: on unpause the next call
        # finds the queue exactly where it was.
        if self._paused:
            outdata.fill(0)
            return

        # Seeking → also output silence. Without this, the brief
        # window where the old decoder has exited and the new one
        # hasn't started would let the callback raise CallbackStop
        # and end the stream mid-seek.
        if self._seeking:
            outdata.fill(0)
            return

        # A pipeline swap is rewriting decoder/queue/gain refs on
        # another thread. This callback is lock-free, so reading a
        # half-swapped pipeline would feed the new track's samples
        # through the old track's ReplayGain/filters — the
        # split-second "explosion" on track change. Silence until the
        # swap (including the ReplayGain re-derive) completes; it's
        # sub-millisecond, so this is inaudible.
        if self._swapping:
            outdata.fill(0)
            return

        written = 0
        channels = outdata.shape[1]

        # Crossfade: when enabled and the current track is within the fade
        # window, mix the next track in instead of cutting to it. Fully
        # self-contained — when off (the default) or any precondition is
        # unmet, `_xfade_active` stays False and the normal single-queue
        # loop below runs unchanged. Setting `written = frames` skips that
        # loop; the shared output stages (cast/EQ/volume) and the
        # samples_emitted / seq bump below then apply to the mix exactly as
        # they do to normal audio.
        if not self._xfade_active and self._crossfade_s > 0.0:
            self._maybe_begin_crossfade()
        if self._xfade_active:
            self._crossfade_fill(outdata, frames, channels)
            written = frames

        while written < frames:
            if self._callback_carry is None:
                try:
                    chunk = self._pcm_queue.get_nowait()
                except queue.Empty:
                    chunk = None
                if chunk is None:
                    if self._decoder_done.is_set():
                        # Primary track exhausted. If a compatible
                        # preload is waiting, splice it into the
                        # SAME OutputStream — that's the gapless
                        # moment. Re-enter the loop with the new
                        # queue.
                        if self._try_gapless_swap():
                            continue
                        outdata[written:] = 0
                        raise sd.CallbackStop
                    # Underrun — decoder hasn't caught up. Silence
                    # the rest of this callback. Critically we still
                    # advance samples_emitted below so the scrubber
                    # keeps moving through the underrun window —
                    # otherwise position would freeze on every
                    # hiccup. Bump seq on the same wall-clock cadence
                    # as the main path so the frontend's seq-dedup
                    # actually lets the position update through.
                    self._log_callback_starvation()
                    outdata[written:] = 0
                    self._samples_emitted += frames
                    if now - self._seq_last_bump_t >= 0.2:
                        self._seq += 1
                        self._seq_last_bump_t = now
                    return
                self._callback_carry = chunk

            take = min(frames - written, self._callback_carry.shape[0])
            src = self._callback_carry[:take]
            if src.shape[1] == channels:
                outdata[written : written + take] = src
            elif src.shape[1] == 2 and channels == 1:
                outdata[written : written + take, 0] = src.mean(axis=1)
            else:
                outdata[written : written + take] = 0
            written += take
            if take >= self._callback_carry.shape[0]:
                self._callback_carry = None
            else:
                self._callback_carry = self._callback_carry[take:]

        # Cast tap. Runs before EQ / volume / mute so the device
        # gets the raw decoded PCM; the Cast device has its own
        # volume control, so muting
        # locally shouldn't silence the remote speaker. The
        # is_active() probe is lock-free in the common 'no
        # session' case, so the cost when nobody's casting is one
        # attribute read per audio callback. Tidal sources decode
        # to int16 or int32, but on Windows WASAPI shared mode the
        # OutputStream comes out as float32 (device mixer format),
        # so we hand all three through; cast_manager.push_pcm
        # converts float32 to int32 internally for the FLAC encoder.
        if _cast_mod is not None:
            try:
                if _cast_mod.cast_manager.is_active():
                    if outdata.dtype == np.int16:
                        _dtype_name = "int16"
                    elif outdata.dtype == np.int32:
                        _dtype_name = "int32"
                    elif outdata.dtype == np.float32:
                        _dtype_name = "float32"
                    else:
                        _dtype_name = None
                    if _dtype_name is not None:
                        _cast_mod.cast_manager.push_pcm(
                            np.ascontiguousarray(outdata),
                            sample_rate=self._stream_sample_rate or 44100,
                            dtype=_dtype_name,
                        )
            except Exception:
                # Never let Cast errors take down local playback.
                pass

        # UPnP / DLNA tap. Same pre-EQ / pre-volume position and
        # same dtype handling as Cast. The streaming pipeline is
        # FLAC-over-HTTP for both, so the encoder ingestion contract
        # is identical. `is_active()` is a single attribute read,
        # and `upnp_manager` is a module-level singleton (no lock,
        # no lazy-init branch), so the cost when DLNA isn't in use
        # is exactly two attribute reads per audio callback.
        if _upnp_manager is not None:
            try:
                if _upnp_manager.is_active():
                    if outdata.dtype == np.int16:
                        _dtype_name = "int16"
                    elif outdata.dtype == np.int32:
                        _dtype_name = "int32"
                    elif outdata.dtype == np.float32:
                        _dtype_name = "float32"
                    else:
                        _dtype_name = None
                    if _dtype_name is not None:
                        _upnp_manager.push_pcm(
                            np.ascontiguousarray(outdata),
                            sample_rate=self._stream_sample_rate or 44100,
                            dtype=_dtype_name,
                        )
            except Exception:
                # Never let DLNA errors take down local playback.
                pass

        # External output active: silence local. Done AFTER the
        # Cast / Tidal Connect taps above (so the remote
        # receiver gets full-amplitude audio at its own volume
        # control) and BEFORE the volume / mute logic below (so the
        # silencing is unconditional regardless of user volume
        # state). The OutputStream still runs — we just hand it
        # zeros — which keeps the realtime callback driving and
        # avoids the underrun-recovery dance that stopping +
        # restarting would cost when the user toggles back to
        # local.
        if self._external_output_active:
            outdata.fill(0)
            self._samples_emitted += frames
            # Bump seq so the frontend's position scrubber still
            # advances even though local is silent — playback is
            # still happening, just on the remote.
            if now - self._seq_last_bump_t >= 0.2:
                self._seq += 1
                self._seq_last_bump_t = now
            return

        # DSP stages: ReplayGain (scalar gain) → EQ (10-band biquad
        # / AutoEQ) → crossfeed (Bauer stereo imaging). All three
        # operate on float32, so when any of them is active we
        # round-trip through a single float32 buffer instead of
        # paying the int↔float conversion per stage. Bit-perfect
        # pass-through holds when all three are inactive and we
        # never enter this block.
        #
        # RG goes first because it sets the base level the rest of
        # the chain shapes around — applying it after EQ would let
        # an EQ-boosted band push the post-RG peak into clipping.
        rg_active = self._replaygain.is_active()
        eq_active = (
            self._eq is not None
            and self._eq.is_active()
            and not self._eq_bypass
        )
        # Crossfeed only makes sense for stereo; mono / 5.1 pay
        # nothing for the feature being on.
        crossfeed_active = (
            self._crossfeed is not None
            and self._crossfeed.is_active()
            and outdata.ndim == 2
            and outdata.shape[1] == 2
        )
        if rg_active or eq_active or crossfeed_active:
            if outdata.dtype == np.float32:
                if rg_active:
                    self._replaygain.apply(outdata)
                if eq_active:
                    self._eq.apply(outdata)
                if crossfeed_active:
                    self._crossfeed.apply(outdata)
            else:
                # Scale to float32 in the range [-1, 1], filter,
                # scale back. int range constants chosen so the
                # round-trip is exact for unmodified samples.
                scale_in = (
                    32768.0 if outdata.dtype == np.int16 else 2_147_483_648.0
                )
                buf = outdata.astype(np.float32, copy=True) / scale_in
                if rg_active:
                    self._replaygain.apply(buf)
                if eq_active:
                    self._eq.apply(buf)
                if crossfeed_active:
                    self._crossfeed.apply(buf)
                np.clip(buf * scale_in, -scale_in, scale_in - 1.0, out=buf)
                # np.rint rounds to nearest (banker's); plain astype
                # would truncate toward zero and bias samples slightly
                # negative on average.
                outdata[:] = np.rint(buf).astype(outdata.dtype)

        # Volume + mute post-processing. At volume=100 and not muted
        # (and no EQ above), bit-perfect pass-through still holds when
        # the decoder isn't doing internal SRC; when it is, the
        # decoder has already attenuated by ~1 dB upstream so peaks
        # land below full scale.
        if self._muted or self._volume <= 0:
            outdata.fill(0)
        elif self._volume < 100:
            vol = self._volume / 100.0
            if outdata.dtype == np.float32:
                outdata *= vol
            else:
                # int16 / int32: scale via float roundtrip. The float
                # multiplier is safe for a 32-bit int dtype because
                # we clamp in astype().
                scaled = outdata.astype(np.float32, copy=False) * vol
                outdata[:] = scaled.astype(outdata.dtype)

        self._samples_emitted += frames
        # Reached the end having delivered real audio this callback,
        # so the gap to the NEXT callback is a meaningful deadline
        # measurement (see the jitter check at the top).
        self._cb_prev_audio = True
        # Bump seq so the frontend's SSE dedupe lets through the
        # position update. Wall-clock cadence (5 Hz) — independent of
        # the callback's own rate. PortAudio's frames-per-callback
        # depends on the device + buffer configuration: ~512 on a
        # 44.1k Mac (~86 Hz callback), but as low as ~10 Hz callback
        # for hi-res-output configs that prefer larger buffers.
        # The earlier "/20" scheme tracked the 86 Hz case and
        # silently regressed everywhere else — the frontend's
        # seq-dedup would drop multiple 4 Hz SSE pollings between
        # consecutive seq bumps, so the scrubber jumped multiple
        # seconds at a time instead of sliding smoothly.
        if now - self._seq_last_bump_t >= 0.2:
            self._seq += 1
            self._seq_last_bump_t = now

    def _adopt_preload_locked(self, pre: _Preload) -> PlayerSnapshot:
        """Synchronous version of the callback's gapless swap,
        triggered from load() when the frontend fires play_track
        for a track we already have preloaded.

        Must be called with `self._lock` held. Stops the current
        decoder thread, swaps refs to the preload's pipeline, and
        either keeps the OutputStream alive (same rate/dtype —
        gapless) or re-opens it (different rate — small reopen
        gap). Does not tear the preload's decoder thread: it keeps
        running against its own queue, which now becomes the
        primary queue.
        """
        # Capture pieces we need to clean up OUTSIDE the lock so we
        # don't hold it during slow I/O (thread.join, decoder.close).
        old_thread = self._decoder_thread
        old_stop_flag = self._stop_flag
        old_decoder = self._decoder
        rate_matches = (
            pre.sample_rate == self._stream_sample_rate
            and pre.sd_dtype == self._stream_sd_dtype
            and pre.channels == self._stream_channels
        )
        old_stream = None if rate_matches else self._stream

        # Adopt the preload's pipeline. Callback is free to read
        # from the new queue the instant after these assignments.
        # `_swap_pipeline_to` writes rate/dtype/channels
        # unconditionally; that's a no-op in the same-rate case
        # because the values already match the active stream.
        self._swap_pipeline_to(pre)
        # Start DLNA passthrough for the new track.  This runs on
        # the HTTP thread, so blocking on SetAVTransportURI is fine.
        if (
            _upnp_manager is not None
            and _upnp_manager.is_active()
            and pre.source_urls is not None
        ):
            try:
                _upnp_manager.start_passthrough(
                    pre.source_urls,
                    prefetched=None,
                    metadata=self._current_track_meta,
                )
            except (ValueError, RuntimeError, OSError) as exc:
                print(
                    f"[player] upnp start_passthrough failed "
                    f"(adopt_preload): {exc!r}",
                    flush=True,
                )
        self._preload = None
        self._state = "playing"
        self._last_error = None
        self._seq += 1

        # Outside the lock (well — RLock held; cleanup stays
        # short): signal + close the old pipeline's resources.
        old_stop_flag.set()
        # Cancel the old decoder's source BEFORE joining. Same
        # reasoning as _teardown / _restart_decoder_at: stop_flag
        # only gets checked between PCM frames, and the old thread
        # is very often mid-HTTP-fetch on a track change. Closing
        # the source aborts the in-flight read so the join returns
        # in milliseconds instead of waiting the segment out.
        if old_decoder is not None:
            try:
                old_decoder.cancel_source()
            except Exception:
                pass
        if old_thread is not None and old_thread.is_alive():
            # Short bounded join: gives the thread a chance to
            # notice the stop flag and release its PyAV container
            # + SegmentReader session. If it's blocked in network
            # I/O past the timeout we give up (it's a daemon and
            # won't prevent process exit), but we've tried rather
            # than dropping the ref and guaranteeing a ~30s leak.
            old_thread.join(timeout=0.5)
        if old_decoder is not None:
            try:
                old_decoder.close()
            except Exception:
                pass
        if old_stream is not None:
            # abort() over stop() — same reason as _teardown above:
            # stop() drains the buffer and stalls the cross-rate
            # bridge by half a second. Any tail audio we discard
            # is about to be drowned by the new stream anyway.
            try:
                old_stream.abort()
            except Exception:
                pass
            try:
                old_stream.close()
            except Exception:
                pass
            # Re-open at the new rate.
            self._open_output_stream(
                pre.sample_rate, pre.channels, pre.sd_dtype
            )
            try:
                self._stream.start()
            except Exception as exc:
                log.exception("adopt_preload: stream.start failed")
                self._last_error = str(exc)
                self._state = "error"
                self._seq += 1

        self._emit()
        return self.snapshot()

    @staticmethod
    def _pull_block(q, carry, done_event, n: int, channels: int):
        """Pull up to `n` frames from (queue, carry) into a fresh
        (n, channels) float32 buffer for crossfade mixing.

        Returns (buf, new_carry, frames_filled, source_done). Unfilled
        frames are left as zeros — either a transient underrun or the
        source ending. `source_done` is True ONLY when the queue is empty
        AND `done_event` is set (a true end, not a hiccup), which is how
        the caller distinguishes "fade finished because the outgoing track
        ran out" from "decoder briefly behind".
        """
        buf = np.zeros((n, channels), dtype=np.float32)
        filled = 0
        source_done = False
        while filled < n:
            if carry is None:
                try:
                    chunk = q.get_nowait()
                except queue.Empty:
                    chunk = None
                if chunk is None:
                    source_done = done_event.is_set()
                    break
                carry = chunk
            take = min(n - filled, carry.shape[0])
            src = carry[:take]
            # Normalize integer PCM to float32 [-1, 1] so the equal-power
            # mix math works regardless of stream dtype. float32 chunks pass
            # through as-is (already in range).
            if src.dtype == np.int32:
                src = src.astype(np.float32) / 2147483648.0
            elif src.dtype == np.int16:
                src = src.astype(np.float32) / 32768.0
            if src.shape[1] == channels:
                buf[filled : filled + take] = src
            elif src.shape[1] == 2 and channels == 1:
                buf[filled : filled + take, 0] = src.mean(axis=1)
            # else: channel mismatch can't reach here (the crossfade is
            # gated on the incoming channel count matching the stream);
            # leave zeros rather than risk a malformed write.
            filled += take
            if take >= carry.shape[0]:
                carry = None
            else:
                carry = carry[take:]
        return buf, carry, filled, source_done

    def _maybe_begin_crossfade(self) -> None:
        """Start a crossfade if the current track is within the configured
        fade window and a compatible, sufficiently-buffered preload is
        ready. Realtime-safe: claims the preload under a NON-blocking lock
        (same pattern as `_try_gapless_swap`) and bails on any contention
        or unmet precondition, leaving normal playback untouched. Only ever
        called from the audio callback."""
        cf = self._crossfade_s
        if cf <= 0.0 or self._exclusive_mode:
            return
        rate = self._stream_sample_rate or 0
        if rate <= 0 or self._current_duration_ms <= 0:
            return
        total = int(cf * rate)
        if total <= 0:
            return
        track_samples = int(self._current_duration_ms / 1000.0 * rate)
        if track_samples - self._samples_emitted > total:
            return  # not in the fade window yet
        if self._preload is None:
            return
        if not self._lock.acquire(blocking=False):
            return  # contended — try again next callback
        try:
            pre = self._preload
            if pre is None:
                return
            if (
                pre.sample_rate != self._stream_sample_rate
                or pre.sd_dtype != self._stream_sd_dtype
                or pre.channels != self._stream_channels
            ):
                return  # incompatible — let the gapless/bridge path handle it
            if (
                pre.queue.qsize() < _XFADE_MIN_PREBUFFER_CHUNKS
                and not pre.done.is_set()
            ):
                return  # not buffered enough yet; re-check next callback
            # Claim it: _drop_preload / _try_gapless_swap won't touch it now.
            self._preload = None
        finally:
            self._lock.release()
        self._xfade_in = pre
        self._xfade_carry_in = None
        self._xfade_pos = 0
        self._xfade_total = total
        self._xfade_active = True
        print(
            f"[crossfade] begin -> incoming={pre.track_id} over {cf:.1f}s ({total} samples)",
            flush=True,
        )

    def _crossfade_fill(self, outdata, frames: int, channels: int) -> None:
        """Fill `outdata` with one callback's equal-power mix of the
        outgoing (current) and incoming (claimed preload) tracks, advance
        the fade, and promote the incoming track to primary once the fade
        completes or the outgoing track ends. Works for any stream dtype
        (float32, int32, int16): _pull_block normalizes chunks to float32
        for mixing, then the result is scaled back to the stream dtype
        before writing to outdata. Does NOT touch `_samples_emitted` /
        `_seq` — the shared post-fill below the call site handles those."""
        pre = self._xfade_in
        if pre is None:
            # Defensive — never mix without an incoming source.
            self._xfade_active = False
            outdata.fill(0)
            return
        out_buf, self._callback_carry, _of, out_done = self._pull_block(
            self._pcm_queue, self._callback_carry, self._decoder_done,
            frames, channels,
        )
        in_buf, self._xfade_carry_in, _if, _in_done = self._pull_block(
            pre.queue, self._xfade_carry_in, pre.done, frames, channels,
        )
        mixed = mix_crossfade_block(out_buf, in_buf, self._xfade_pos, self._xfade_total)
        # _pull_block normalizes all dtypes to float32 [-1, 1]; scale back
        # to the stream dtype before writing. float32 streams get a direct
        # copy; integer streams are scaled and clipped to their full range.
        if outdata.dtype == np.float32:
            outdata[:] = mixed
        elif outdata.dtype == np.int32:
            outdata[:] = np.clip(
                mixed * 2147483648.0, -2147483648.0, 2147483647.0
            ).astype(np.int32)
        elif outdata.dtype == np.int16:
            outdata[:] = np.clip(
                mixed * 32768.0, -32768.0, 32767.0
            ).astype(np.int16)
        self._xfade_pos += frames
        # Fade complete, or the outgoing track ran out before it finished
        # (the window started a hair late, or a short track) — promote the
        # incoming track either way.
        if self._xfade_pos >= self._xfade_total or out_done:
            self._promote_crossfade_incoming()

    def _promote_crossfade_incoming(self) -> None:
        """Make the faded-in track the primary pipeline. Mirrors
        `_try_gapless_swap`'s ended -> swap -> playing emit so the
        frontend's advance logic wires up the new track, then restores the
        incoming track's already-elapsed position + carry (which
        `_swap_pipeline_to` zeroes). Runs on the realtime callback thread,
        so it never JOINs the outgoing decoder — it signals it and lets the
        daemon thread exit (the same deadlock avoidance the file relies on
        everywhere)."""
        pre = self._xfade_in
        if pre is None:
            self._xfade_active = False
            return
        elapsed = self._xfade_pos
        carry_in = self._xfade_carry_in
        old_decoder = self._decoder
        old_stop = self._stop_flag
        print(f"[crossfade] promote -> {pre.track_id}", flush=True)
        self._state = "ended"
        self._seq += 1
        self._emit()
        self._swap_pipeline_to(pre)
        # The incoming track already played `elapsed` samples during the
        # fade; _swap_pipeline_to reset these to 0 / None.
        self._samples_emitted = elapsed
        self._callback_carry = carry_in
        self._state = "playing"
        self._seq += 1
        self._emit()
        self._xfade_active = False
        self._xfade_in = None
        self._xfade_carry_in = None
        self._xfade_pos = 0
        self._xfade_total = 0
        # Tear down the outgoing decoder WITHOUT joining (realtime thread).
        if old_stop is not None:
            old_stop.set()
        if old_decoder is not None:
            try:
                old_decoder.cancel_source()
            except Exception:
                pass

    def _abort_crossfade(self) -> None:
        """Cancel an in-progress (or claimed-but-not-yet-started)
        crossfade and dispose the incoming preload we'd claimed. Called
        from the pipeline-reset paths (teardown / seek). A manual skip,
        stop, or seek inside the fade window would otherwise leak the
        claimed decoder and leave stale fade state. Safe to call when no
        crossfade is active.

        Disposal is by `stop_flag` + `cancel_source` only — both are
        designed to be called concurrently with the decoder thread (the
        teardown path does the same on the primary decoder). We do NOT
        synchronously `close()` the decoder here: a callback could still
        be reading the claimed preload's already-decoded queue, and the
        daemon decoder thread exits on the stop flag and GCs on its own."""
        pre = self._xfade_in
        self._xfade_active = False
        self._xfade_in = None
        self._xfade_carry_in = None
        self._xfade_pos = 0
        self._xfade_total = 0
        if pre is not None:
            try:
                pre.stop_flag.set()
            except Exception:
                pass
            try:
                pre.decoder.cancel_source()
            except Exception:
                pass

    def _try_gapless_swap(self) -> bool:
        """Called from the audio callback when the current queue is
        empty + primary decoder is done. Swaps in the preloaded
        track's decoder/queue/thread if the sample rate + dtype +
        channel count match. Returns True on success; False if
        there's no preload or it's incompatible.

        Emits `ended` BEFORE the swap and `playing` after. The
        frontend's advance logic fires on `ended`, updates its
        `expectedTrackIdRef` via `playAtIndex`, and the redundant
        `play_track(next)` that follows lands on Path 0 (no-op
        because current_track_id already matches). Without the
        `ended` emit, the frontend's expected-guard would drop all
        new snapshots because its expected is still the previous
        track.

        Assignments are individually atomic under the GIL; the
        callback is the sole modifier during the track-boundary
        moment.
        """
        # Atomically capture + clear the preload slot so a racing
        # `_drop_preload` on the HTTP thread can't close the
        # preload's decoder between our capability check below and
        # the ref swap. The lock is held only for the pointer grab
        # (microseconds) — not during the actual swap or emit.
        #
        # CRITICAL: this runs on the PortAudio realtime callback
        # thread, and _lock is also held by the load/adopt path
        # while it join()s decoder threads that can stall on a
        # network segment read. A *blocking* acquire here parks the
        # realtime thread on that lock until the join unwinds —
        # which, if the decoder is wedged on a hung fetch, is
        # forever: audio dies with no log line and every thread ends
        # up in a cond-wait (the silent freeze). So acquire
        # non-blocking only; on contention, bail and let the
        # off-realtime _on_stream_finished bridge handle the
        # transition (a momentary gap instead of a permanent
        # deadlock). This restores the "the audio callback is
        # lock-free" invariant the rest of this file relies on.
        if not self._lock.acquire(blocking=False):
            return False
        try:
            pre = self._preload
            if pre is None:
                return False
            if (
                pre.sample_rate != self._stream_sample_rate
                or pre.sd_dtype != self._stream_sd_dtype
                or pre.channels != self._stream_channels
            ):
                # Incompatible — fall through to CallbackStop and
                # let _on_stream_finished spawn the bridge thread.
                return False
            self._preload = None
        finally:
            self._lock.release()
        log.info(
            "gapless swap (inline) -> track=%s rate=%dHz dtype=%s",
            pre.track_id, pre.sample_rate, pre.sd_dtype,
        )
        # Phase 1: emit `ended` so the frontend's advance logic
        # wires up expectedTrackIdRef for the new track.
        self._state = "ended"
        self._seq += 1
        self._emit()
        # Phase 2: actual swap. Old decoder + thread have already
        # finished (done event is set), we just replace refs.
        self._swap_pipeline_to(pre)
        # Defer UPnP passthrough notification: the audio callback
        # can't block on HTTP (SetAVTransportURI).  The flag is
        # picked up by _load_locked() Path 0 when the frontend's
        # play_track() arrives.
        if (
            _upnp_manager is not None
            and _upnp_manager.is_active()
            and pre.source_urls is not None
        ):
            self._pending_upnp_notify = True
        self._state = "playing"
        self._seq += 1
        self._emit()
        return True

    def _on_stream_finished(self) -> None:
        # Called by sounddevice when the stream ends. Three reasons it
        # might fire:
        #   1. Natural EOF — callback raised CallbackStop because the
        #      decoder finished and the queue drained.
        #   2. Intentional stream replacement (set_output_device,
        #      set_exclusive_mode, cross-rate bridge) — `_replacing_
        #      stream` is set; ignore this firing entirely, the
        #      replacement code is opening a new stream right now.
        #   3. Device loss — headphones unplugged, USB DAC pulled,
        #      Bluetooth out of range. PortAudio aborts the stream
        #      because the underlying CoreAudio device is gone, but
        #      our decoder still has frames buffered. Recover by
        #      reopening on the system default so playback continues
        #      on speakers without pausing — that's how every other
        #      streaming app behaves and what users expect.
        #
        # Capture AND clear the preload pointer under the same lock
        # acquisition so a concurrent _drop_preload on the HTTP
        # thread (e.g. if the user also clicked Stop at the same
        # microsecond as natural EOF) cannot close the preload's
        # decoder between our read of self._preload and the bridge
        # thread's use of it. We "own" the preload for the duration
        # of the bridge attempt; if the bridge fails, it's
        # responsible for disposing of the decoder itself.
        with self._lock:
            if self._state in ("idle", "error"):
                return
            if self._replacing_stream:
                # Intentional stream swap — see set_output_device /
                # set_exclusive_mode. Don't transition state; don't
                # trigger device-loss recovery.
                return
            # Device-loss heuristic: stream ended unexpectedly, with
            # work still pending. Two ways to know the work isn't done:
            #
            #   1. Decoder hasn't finished decoding the rest of the
            #      track (`not decoder_done`).
            #   2. Decoder IS done but the PCM queue still has chunks
            #      the callback never got to render (`qsize > 0`). This
            #      is the case when the device disappears late in a
            #      short track — the decoder finished filling the queue
            #      before the unplug, so `decoder_done` is True even
            #      though several seconds of audio are still waiting to
            #      play.
            #
            # Natural EOF, by contrast, has both the decoder done AND
            # the queue drained — the callback got everything out
            # before raising CallbackStop.
            decoder_done = self._decoder_done.is_set()
            queue_pending = (
                self._pcm_queue.qsize() > 0
                if self._pcm_queue is not None
                else False
            )
            was_playing = self._state == "playing"
            pre = self._preload
            should_recover = was_playing and pre is None and (
                not decoder_done or queue_pending
            )
            if should_recover:
                log.info(
                    "device-loss detected: triggering recovery "
                    "(decoder_done=%s, queue_pending=%s)",
                    decoder_done, queue_pending,
                )
                # Recovery runs off-thread because the finished_callback
                # runs on sounddevice's own thread which we shouldn't
                # re-enter stream machinery from.
                threading.Thread(
                    target=self._recover_from_device_loss,
                    name="pcm-device-recovery",
                    daemon=True,
                ).start()
                return
            self._preload = None

        # Cross-rate bridge: preload exists but has a different
        # sample rate / dtype than the current stream, so the
        # callback-level gapless swap was skipped. Re-open the
        # OutputStream with the preload's params. Off-thread
        # because sounddevice's finished_callback runs on its
        # own thread which shouldn't re-enter stream machinery.
        if pre is not None:
            threading.Thread(
                target=self._bridge_to_preload,
                args=(pre,),
                name="pcm-bridge",
                daemon=True,
            ).start()
            return

        # Natural EOF. PortAudio's `paComplete` puts the stream
        # into a terminal "stopped" state that `Pa_StartStream`
        # cannot revive — the docs say it has to be reopened. If
        # we leave `self._stream` pointing at this corpse, the
        # next `load()` snapshots it as `active_stream`, takes
        # path (A) ("kept stream open"), and rebinds the new
        # pipeline against a dead OutputStream. `play()` calls
        # `stream.start()` on macOS, which returns without error
        # but never re-wakes the CoreAudio callback thread. The
        # decoder fills the queue, blocks on `put`, and the
        # stall watchdog reports `state=playing, silent`. Close
        # and drop the ref here so the next load takes the full
        # reopen path (B) — that path already works correctly.
        old_stream = self._stream
        with self._lock:
            self._stream = None
            self._transition("ended")
        if old_stream is not None:
            try:
                old_stream.close()
            except Exception:
                pass
        # Signal source_done to the DLNA passthrough encoder so the
        # renderer sees a clean end-of-stream instead of hanging on
        # the HTTP connection until the 30s write timeout kicks in.
        # Only when no preload exists — if one is pending, the gapless
        # transition will start passthrough for the next track.
        if _upnp_manager is not None:
            try:
                if _upnp_manager.is_active():
                    _upnp_manager.signal_source_done()
            except Exception as exc:
                print(
                    f"[player] upnp signal_source_done failed: {exc!r}",
                    flush=True,
                )
        self._emit()

    def _recover_from_device_loss(self) -> None:
        """Re-bind the output stream to a working device after a
        CoreAudio routing change. Two scenarios this covers:

          A. The currently-selected device disappeared (headphones
             unplugged, USB DAC removed, AirPods drifted out of
             range). resolve_output_device reports it gone, so we
             drop the selection and reopen on the system default.

          B. The device list changed but the selected device is
             still around (a NEW device just plugged in — e.g.
             headphones reconnected while audio is on speakers).
             The reopen on the original device succeeds; the user's
             selection is preserved AND PortAudio's enumeration
             gets refreshed in the same step so the picker shows
             the new device next time it opens.

        Standard streaming-app behavior: unplug headphones, audio
        keeps playing on speakers; plug them back in, the picker
        sees them again. ~50 ms of silence while CoreAudio
        renegotiates, same as a manual device switch.

        The decoder thread keeps feeding the PCM queue throughout,
        so once we open a fresh stream the first callback drains
        the queued frames and audio resumes seamlessly.
        """
        with self._pipeline_lock:
            with self._lock:
                # Bail if state moved on — user pressed stop, a
                # natural EOF arrived, error path fired, etc.
                if self._state in ("idle", "error", "ended"):
                    return
                if self._stream is None:
                    return
                sample_rate = self._stream_sample_rate
                channels = self._stream_channels
                sd_dtype = self._stream_sd_dtype
                old_stream = self._stream
                self._stream = None
                self._callback_carry = None
                # KEEP the user's selection. We try it first below;
                # only fall back to default if the reopen fails
                # (which is what means "the device actually
                # disappeared" vs "device list just changed").
                original_device_id = self._selected_device_id
                self._replacing_stream = True

            if (
                sample_rate is None
                or channels is None
                or sd_dtype is None
            ):
                with self._lock:
                    self._replacing_stream = False
                return

            try:
                old_stream.close()
            except Exception:
                pass

            # Force PortAudio to re-enumerate CoreAudio. Without
            # this, devices that plugged in since the last init
            # are invisible to PortAudio even though CoreAudio
            # sees them.
            try:
                sd._terminate()
            except Exception:
                pass
            try:
                sd._initialize()
            except Exception:
                log.exception("device-recovery: PortAudio reinit failed")

            # _open_output_stream now falls back to the system default
            # on its own when the saved device is gone (it no longer
            # raises), so scenario A can't be inferred from an exception
            # any more. Ask the resolver directly: if the selection
            # really vanished, drop it up front so the picker and status
            # stop reporting a device that isn't there, and the reopen
            # below lands on the default. A present-but-unopenable device
            # still falls through to the second attempt via the except.
            _, device_available = resolve_output_device(original_device_id)
            device_gone = bool(original_device_id) and not device_available
            if device_gone:
                with self._lock:
                    self._selected_device_id = ""

            # First attempt: the resolved device (scenario B), or the
            # system default when the pick vanished (scenario A).
            try:
                with self._lock:
                    self._open_output_stream(
                        sample_rate, channels, sd_dtype
                    )
                self._stream.start()
                if device_gone:
                    log.info(
                        "device-recovery: original device id=%r gone; "
                        "reopened on system default (rate=%d, channels=%d)",
                        original_device_id, sample_rate, channels,
                    )
                else:
                    log.info(
                        "device-recovery: reopened on device id=%r "
                        "(rate=%d, channels=%d)",
                        self._selected_device_id or "system default",
                        sample_rate, channels,
                    )
                with self._lock:
                    self._replacing_stream = False
                return
            except Exception as exc:
                log.info(
                    "device-recovery: original device id=%r "
                    "unavailable (%r); falling back to system default",
                    original_device_id, exc,
                )
                # Reset state for the second attempt.
                with self._lock:
                    self._selected_device_id = ""
                    self._stream = None
                    self._callback_carry = None

            # Second attempt: system default (scenario A: device
            # actually disappeared).
            try:
                with self._lock:
                    self._open_output_stream(
                        sample_rate, channels, sd_dtype
                    )
                self._stream.start()
                log.info(
                    "device-recovery: reopened on system default "
                    "(rate=%d, channels=%d)",
                    sample_rate, channels,
                )
            except Exception:
                log.exception(
                    "device-recovery: system-default fallback also failed"
                )
                with self._lock:
                    self._transition("error")
                    self._stream = None
                self._emit()
            finally:
                with self._lock:
                    self._replacing_stream = False

    def _bridge_to_preload(self, pre: _Preload) -> None:
        """Stream re-open + swap for a cross-rate preload. Produces
        ~50ms of silence between tracks — same limitation every
        gapless player has when sample rates change.

        Serialized on `_pipeline_lock` so a concurrent `stop()` /
        `load()` on the HTTP thread can't run teardown while we're
        mid-bridge. If teardown drove us to idle before we acquired
        the lock, the preload we captured may have been dropped —
        bail out and let the user-initiated state stand.
        """
        with self._pipeline_lock:
            with self._lock:
                if self._state == "idle":
                    # Teardown ran between _on_stream_finished and
                    # here. Our captured `pre` may have been closed
                    # by _drop_preload; don't try to adopt it.
                    try:
                        pre.stop_flag.set()
                        pre.decoder.close()
                    except Exception:
                        pass
                    return

            # Mirror `_try_gapless_swap`'s pattern: emit `ended` for
            # the OUTGOING track BEFORE swapping. The frontend's
            # advance logic keys off this snapshot to wire its
            # `expectedTrackIdRef` up to the new track id; without
            # the emit, every subsequent snapshot for the new track
            # fails the late-echo guard and the now-playing bar gets
            # stuck on the previous track even though audio has
            # moved on. Same-rate transitions don't hit this path
            # (they go through `_try_gapless_swap` which already
            # emits `ended`), so the bug only surfaced on cross-rate
            # transitions — typically album → Artist Radio where
            # the radio tracks have a different sample rate than
            # the album that just finished.
            with self._lock:
                self._state = "ended"
                self._seq += 1
            self._emit()

            log.info(
                "gapless bridge (cross-rate) -> track=%s rate %d->%dHz dtype %s->%s",
                pre.track_id,
                self._stream_sample_rate or 0,
                pre.sample_rate,
                self._stream_sd_dtype,
                pre.sd_dtype,
            )
            # The old stream already fired finished_callback, so it's
            # stopped. Close it for good measure and release
            # resources.
            old_stream = self._stream
            self._stream = None
            if old_stream is not None:
                try:
                    old_stream.close()
                except Exception:
                    pass
            # Close the old decoder (thread has already exited since
            # decoder_done fired).
            old_decoder = self._decoder
            if old_decoder is not None:
                try:
                    old_decoder.close()
                except Exception:
                    pass

            with self._lock:
                # Clear the preload slot BEFORE opening a new
                # stream so a racing preload() call (unlikely this
                # early) can't re-stomp it.
                self._preload = None
                self._swap_pipeline_to(pre)
                # Start DLNA passthrough for the new track.  The
                # bridge thread is off-realtime, so HTTP is fine.
                if (
                    _upnp_manager is not None
                    and _upnp_manager.is_active()
                    and pre.source_urls is not None
                ):
                    try:
                        _upnp_manager.start_passthrough(
                            pre.source_urls,
                            prefetched=None,
                            metadata=self._current_track_meta,
                        )
                    except (ValueError, RuntimeError, OSError) as exc:
                        print(
                            f"[player] upnp start_passthrough failed "
                            f"(bridge): {exc!r}",
                            flush=True,
                        )
                self._open_output_stream(
                    pre.sample_rate, pre.channels, pre.sd_dtype
                )
                self._transition("playing")
            try:
                self._stream.start()
            except Exception:
                log.exception("bridge: stream.start failed")
                with self._lock:
                    self._last_error = "stream reopen failed"
                    self._transition("error")
                self._emit()
                return
            self._emit()

    def _drain_queue_locked(self) -> None:
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                break

    def _transition(self, state: str, track_id: Optional[str] = None) -> None:
        self._state = state
        if track_id is not None:
            self._current_track_id = track_id
        self._seq += 1

    def _emit(self) -> None:
        snap = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(snap)
            except Exception:
                log.exception("listener raised")

    def _swap_pipeline_to(self, pre: "_Preload") -> None:
        """Replace the active pipeline refs with those from `pre`.

        Single source of truth for the field copies that drive a
        track swap. Same shape was hand-rolled in three places
        (_try_gapless_swap, _bridge_to_preload, _adopt_preload_locked)
        and inevitably drifted — the cross-rate gapless bug we just
        shipped a fix for came from one of those copies forgetting a
        partner emit. Centralising it here means a new field on
        `_Preload` only has to be wired up once.

        Atomicity: each assignment is one bytecode op under the GIL,
        but the *sequence* is not atomic, and the audio callback is
        lock-free — holding `_lock` (as `_adopt_preload_locked` does)
        does NOT keep the callback out, so it could read a
        half-swapped pipeline: the new track's queue with the old
        track's ReplayGain and filter state. That mismatch is the
        split-second "explosion" on track change. The `_swapping`
        guard below closes it: it's set before the first ref changes
        and cleared after the ReplayGain re-derive, and the callback
        emits silence the whole time (sub-millisecond, inaudible).
        `_try_gapless_swap` runs in the callback thread itself, so
        the guard there is just set and cleared within one
        invocation — harmless, and keeps a single code path.

        Always copies the rate / dtype / channels triple along with
        the rest. Same-rate swaps no-op those (the values match the
        active stream); the alternative — conditional copies — was
        the contract drift this helper exists to prevent.
        """
        old_track = self._current_track_id
        old_info = self._current_stream_info
        self._swapping = True
        try:
            self._decoder = pre.decoder
            self._pcm_queue = pre.queue
            self._decoder_thread = pre.thread
            self._stop_flag = pre.stop_flag
            self._decoder_done = pre.done
            self._current_track_id = pre.track_id
            self._current_duration_ms = pre.duration_ms
            self._current_stream_info = pre.stream_info
            self._source_urls = pre.source_urls
            self._source_path = pre.source_path
            self._stream_sample_rate = pre.sample_rate
            self._stream_sd_dtype = pre.sd_dtype
            self._stream_channels = pre.channels
            self._samples_emitted = 0
            self._callback_carry = None
            # Re-derive ReplayGain off the new track's tags while the
            # callback is still silenced, so it never applies the old
            # gain to the new samples.
            self._apply_replaygain_for(pre.stream_info)
        finally:
            self._swapping = False
        # Logged after the guard clears so the file write is outside
        # the silence window. This is the line that was missing when
        # the explosion couldn't be traced from logs: it records the
        # ReplayGain delta across the swap, which is what drives the
        # level jump when the race is open.
        def _rg(info) -> str:
            if info is None:
                return "n/a"
            t = getattr(info, "track_replay_gain_db", None)
            a = getattr(info, "album_replay_gain_db", None)
            return f"track={t} album={a}"

        try:
            audio_log.info(
                "swap %s -> %s | rg old(%s) new(%s)",
                old_track,
                pre.track_id,
                _rg(old_info),
                _rg(pre.stream_info),
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Stream-info helpers (mutagen probe for local files)
# ---------------------------------------------------------------------------


def _safe_int(v: object) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _replaygain_tags_from(info: Optional[StreamInfo]) -> ReplayGainTags:
    """Lift the four ReplayGain numbers off a StreamInfo into the
    explicit tag dataclass the gain engine consumes. Returns an
    all-None tags object when info is None (idle player) so callers
    can apply unconditionally without an extra null check."""
    if info is None:
        return ReplayGainTags()
    return ReplayGainTags(
        track_gain_db=info.track_replay_gain_db,
        track_peak=info.track_peak,
        album_gain_db=info.album_replay_gain_db,
        album_peak=info.album_peak,
    )


def _safe_float(v: object) -> Optional[float]:
    if v is None:
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    # NaN / inf would silently propagate into the gain math and either
    # mute audio (multiply by 0 from a NaN clamp) or blow it up
    # (multiply by inf). Treat them as missing so the caller sees None
    # and decides — the standard "default to 0 dB / unity" path.
    if out != out or out == float("inf") or out == float("-inf"):
        return None
    return out


def _normalize_codec(raw: object) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "flac" in s:
        return "flac"
    if "alac" in s:
        return "alac"
    if "mp4a" in s or "aac" in s:
        return "aac"
    if "mp3" in s:
        return "mp3"
    if "opus" in s:
        return "opus"
    if "vorbis" in s:
        return "vorbis"
    return s


def _build_source(
    spec: Union[str, list[str]],
    prefetched: Optional[dict[int, bytes]] = None,
) -> Union[str, SegmentReader]:
    """Materialize a source spec into something Decoder can open.
    If the caller already fetched the first few segments via the
    byte-level prefetch path, hand them to SegmentReader so it
    skips the corresponding network fetches."""
    if isinstance(spec, str):
        return spec
    return SegmentReader(spec, prefetched=prefetched)


def _probe_local_stream_info(
    path: str,
) -> tuple[Optional[StreamInfo], Optional[float]]:
    """Read codec/duration metadata for a local file. Returns
    `(info, duration_seconds)`; either or both can be None when
    mutagen can't read the file.

    The duration is what makes seek work on local files. Before
    this returned just the StreamInfo, `_resolve_source` passed
    `None` for duration to the player, which left
    `_current_duration_ms = 0` and made `PCMPlayer.seek` bail at
    its `duration_ms <= 0` guard — the user dragged the scrubber
    and the audio kept playing wherever it was."""
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:
        return None, None
    try:
        m = MutagenFile(path)
    except Exception:
        return None, None
    if m is None or getattr(m, "info", None) is None:
        return None, None
    info = m.info
    codec: Optional[str] = None
    mime_list = getattr(info, "mime", None) or []
    if mime_list:
        codec = _normalize_codec(mime_list[0])
    if codec is None:
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        codec = _normalize_codec(ext)
    rg = _read_local_replaygain(m)
    raw_length = getattr(info, "length", None)
    try:
        duration_s = float(raw_length) if raw_length else None
    except (TypeError, ValueError):
        duration_s = None
    stream_info = StreamInfo(
        source="local",
        codec=codec,
        bit_depth=_safe_int(getattr(info, "bits_per_sample", None)),
        sample_rate_hz=_safe_int(getattr(info, "sample_rate", None)),
        track_replay_gain_db=rg.track_gain_db,
        track_peak=rg.track_peak,
        album_replay_gain_db=rg.album_gain_db,
        album_peak=rg.album_peak,
    )
    return stream_info, duration_s


def _read_local_replaygain(m) -> ReplayGainTags:
    """Pull ReplayGain values out of mutagen tags so downloaded
    tracks get the same loudness-leveling treatment as streamed ones.

    Standard tag names per format:
      - FLAC / Vorbis: REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK,
        REPLAYGAIN_ALBUM_GAIN, REPLAYGAIN_ALBUM_PEAK (uppercase).
      - MP3 (ID3): TXXX:replaygain_track_gain etc. (case-insensitive).
      - MP4 (iTunes-style): ----:com.apple.iTunes:replaygain_track_gain.

    Mutagen normalises tag access enough that a flat dict-style lookup
    covers FLAC + Vorbis + MP4 in one path. ID3 needs explicit TXXX
    frame iteration. Anything we can't parse falls through to an
    all-None tags object — the resolver treats that as "no data" and
    skips the gain stage for the track, matching streaming behaviour.
    """
    tags: ReplayGainTags = ReplayGainTags()
    raw_tags = getattr(m, "tags", None)
    if raw_tags is None:
        return tags

    def _coerce_gain(value: object) -> Optional[float]:
        if value is None:
            return None
        # Most formats store "-3.45 dB". Strip the unit and parse.
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        s = str(value).strip()
        if not s:
            return None
        if s.lower().endswith(" db"):
            s = s[:-3].strip()
        elif s.lower().endswith("db"):
            s = s[:-2].strip()
        try:
            return float(s)
        except ValueError:
            return None

    def _coerce_peak(value: object) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        try:
            return float(str(value).strip())
        except ValueError:
            return None

    # FLAC / Vorbis / MP4 — uppercase keys via mutagen's dict-like
    # tag accessor. Case-insensitive lookup falls out of trying both.
    def _get(key: str) -> object:
        for k in (key, key.upper(), key.lower()):
            try:
                v = raw_tags.get(k) if hasattr(raw_tags, "get") else raw_tags[k]
            except (KeyError, TypeError):
                v = None
            if v is not None:
                return v
        return None

    tags.track_gain_db = _coerce_gain(_get("replaygain_track_gain"))
    tags.track_peak = _coerce_peak(_get("replaygain_track_peak"))
    tags.album_gain_db = _coerce_gain(_get("replaygain_album_gain"))
    tags.album_peak = _coerce_peak(_get("replaygain_album_peak"))

    # ID3 path: scan TXXX frames if we didn't find anything above. MP3s
    # written by various taggers use slightly different casing (Tidal's
    # FLAC→MP3 converters, lame --replaygain-fast, foobar2000) but the
    # frame description is always one of these four canonical strings.
    if tags.track_gain_db is None and hasattr(raw_tags, "getall"):
        try:
            txxx_frames = raw_tags.getall("TXXX") or []
        except Exception:
            txxx_frames = []
        for frame in txxx_frames:
            desc = (getattr(frame, "desc", "") or "").lower()
            text = getattr(frame, "text", None)
            value = text[0] if isinstance(text, (list, tuple)) and text else text
            if desc == "replaygain_track_gain" and tags.track_gain_db is None:
                tags.track_gain_db = _coerce_gain(value)
            elif desc == "replaygain_track_peak" and tags.track_peak is None:
                tags.track_peak = _coerce_peak(value)
            elif desc == "replaygain_album_gain" and tags.album_gain_db is None:
                tags.album_gain_db = _coerce_gain(value)
            elif desc == "replaygain_album_peak" and tags.album_peak is None:
                tags.album_peak = _coerce_peak(value)

    return tags
