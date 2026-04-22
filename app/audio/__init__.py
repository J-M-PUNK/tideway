"""PyAV + sounddevice audio engine.

Replaces app/player.py's libvlc-backed engine. The major difference:
we pre-decode the next track's PCM frames into an in-memory buffer
while the current track is still playing, then splice at the sample
boundary for true gapless transitions.

Phase 2 ships `SegmentReader`, `Decoder`, and a minimal `PCMPlayer`
that can load → play → stop a single Tidal track. Pause/seek/volume/
EQ/device-select/local-files/preload land in later phases.
"""
