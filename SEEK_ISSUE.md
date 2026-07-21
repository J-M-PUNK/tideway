### Seek breaks DLNA passthrough

**Description:**  
When the user seeks (scrubs) to a new position in the current track during DLNA passthrough playback, the passthrough encoder is not reset. The renderer (UAPP) continues receiving FLAC frames from the pre-seek position, because the ring buffer and encoder state are never told about the seek.

**Expected behavior:**  
Seek during passthrough should either:
1. Restart passthrough for the current track at the new position (tear down encoder, rebuild SegmentReader at seek target, notify renderer via SetAVTransportURI with updated `?ts=` URL), or
2. Gracefully fall back to PCM re-encode mode for the remainder of the track, then resume passthrough on the next track.

**Root cause:**  
`PCMPlayer.seek()` calls `_restart_decoder_at()` which rebuilds the decoder, but the DLNA passthrough encoder (`FlacPassthroughEncoder`) is not touched — it keeps streaming from the original position's demux point.

**Scope:**  
Medium. Affects UAPP users who scrub during DLNA passthrough. Workaround: skip to next track and back.

**Suggested approach:**  
Add a `seek()` or `restart()` method to `FlacPassthroughEncoder` that tears down the current demux pipeline and rebuilds it at the new byte offset, or gate passthrough off during seek and revert to PCM for the remainder of the track.
