"""PyAV + sounddevice audio engine.

Pre-decodes the next track's PCM frames into an in-memory buffer
while the current track is still playing, then splices at the
sample boundary for true gapless transitions. OutputStream opens
at the track's native sample rate for bit-perfect playback.
"""
