export type ContentKind = "track" | "album" | "artist" | "playlist";

export interface ArtistRef {
  id: string;
  name: string;
  /** Optional artist picture URL. Populated when Tidal embeds the
   *  picture UUID on the track/album payload (common case).
   *  Surfaces used to render the artist-avatar pill. */
  picture?: string | null;
}

export interface Track {
  kind: "track";
  id: string;
  name: string;
  duration: number;
  track_num: number;
  explicit: boolean;
  artists: ArtistRef[];
  album: {
    id: string;
    name: string;
    cover: string | null;
  } | null;
  share_url?: string | null;
  /** Tidal's per-track radio mix id (from raw `mixes.TRACK_MIX`).
   *  When present, "Go to track radio" routes to /mix/:id which has
   *  the composite cover art. Null when Tidal hasn't minted a mix
   *  for this track (rare — new releases, obscure catalog entries). */
  track_mix_id?: string | null;
  /** Quality / codec tags: "HIRES_LOSSLESS" or "LOSSLESS". Frontend
   *  format-filter chip + download-dropdown annotation read these. */
  media_tags?: string[];
  /** International Standard Recording Code. Lets us look up the
   *  same recording on Spotify (for global play counts / artist
   *  monthly listeners). Null/missing for obscure catalog entries
   *  that lack an ISRC registration. */
  isrc?: string | null;
  /** ISO timestamp the track was added to the containing playlist.
   *  Only populated when the Track came through a playlist detail
   *  fetch and Tidal returned a `created` value (user-curated
   *  playlists). Null on editorial / collaborative playlists where
   *  Tidal doesn't expose per-entry timestamps, and on any Track
   *  fetched outside of a playlist context. Drives PlaylistDetail's
   *  "Recently added" sort option. */
  added_at?: string | null;
}

export interface Album {
  kind: "album";
  id: string;
  name: string;
  num_tracks: number;
  year: number | null;
  duration: number;
  cover: string | null;
  artists: ArtistRef[];
  explicit: boolean;
  /** Tidal's streamability verdict (streamReady / allowStreaming).
   *  False = listed but not playable here. Absent on payloads built
   *  before this field existed; treat absent as available. */
  available?: boolean;
  /** Tidal's release classification: "album" | "ep" | "single" |
   *  "compilation". Null when Tidal omits it. */
  album_type?: string | null;
  share_url?: string | null;
  release_date?: string | null;
  copyright?: string | null;
  media_tags?: string[];
}

export interface Artist {
  kind: "artist";
  id: string;
  name: string;
  picture: string | null;
}

export interface Video {
  kind: "video";
  id: string;
  name: string;
  duration: number;
  cover: string | null;
  artist: { id: string; name: string } | null;
  release_date: string | null;
  explicit: boolean;
  quality: string;
  share_url?: string | null;
}

export interface Playlist {
  kind: "playlist";
  id: string;
  name: string;
  description: string;
  num_tracks: number;
  duration: number;
  cover: string | null;
  creator: string | null;
  creator_id?: string | null;
  owned: boolean;
  share_url?: string | null;
}

export interface TidalUser {
  id: string;
  name: string;
  first_name: string;
  last_name: string;
  picture: string | null;
}

export interface PlaylistFolder {
  id: string;
  name: string;
  parent_id: string;
  num_items: number;
}

export type LibraryItem = Track | Album | Artist | Playlist;

/**
 * Snapshot of the backend's native audio player (PyAV + sounddevice).
 * usePlayer mirrors this into its own React state, so the rest of
 * the UI never touches the SSE stream directly.
 */
export interface StreamInfo {
  /** Where the audio is coming from. Local means we're reading a file
   *  off disk; stream means a live Tidal session. */
  source: "stream" | "local";
  codec: string | null;
  bit_depth: number | null;
  sample_rate_hz: number | null;
  /** Tidal's tier string ("HIGH" / "LOSSLESS" / "HI_RES" /
   *  "HI_RES_LOSSLESS"). Only set for streaming sources. */
  audio_quality: string | null;
  /** "STEREO" for anything we can reach via PKCE; immersive modes
   *  (Atmos / 360) aren't authorized on our client_id. */
  audio_mode: string | null;
}

export interface PlayerSnapshot {
  state: "idle" | "loading" | "playing" | "paused" | "ended" | "error";
  track_id: string | null;
  position_ms: number;
  duration_ms: number;
  volume: number;
  muted: boolean;
  error: string | null;
  seq: number;
  /** What's actually audible. Null when the player is idle or still
   *  loading; the UI hides the badge in that case. */
  stream_info: StreamInfo | null;
  /** When true the backend is pinning volume at 100 %. Set via the
   *  "Force volume" option in the bottom bar's output-device menu. */
  force_volume?: boolean;
  /** Set when local playback was paused by a cross-device-pause
   *  event from Tidal's Pushkin bus. Contains the display name of
   *  the device that took over ("iOS", "Desktop", "another device"
   *  as a fallback). Null when no such pause is current.
   *  Cleared automatically when the user resumes playback locally
   *  or explicitly via POST /api/player/dismiss-pause-reason. */
  paused_by_device?: string | null;
}

/** Tidal-nominated single most-relevant result. Discriminated by the
 *  `kind` field already present on every entity. Null when Tidal
 *  didn't pick one (uncommon for popular queries, common for typos)
 *  or when the hit is a type we don't surface (Video). */
export type TopHit = Track | Album | Artist | Playlist;

export interface SearchResponse {
  top_hit: TopHit | null;
  tracks: Track[];
  albums: Album[];
  artists: Artist[];
  playlists: Playlist[];
}

export interface AlbumDetail extends Album {
  tracks: Track[];
  similar: Album[];
  review: string | null;
  more_by_artist: Album[];
  related_artists: Artist[];
}

/** Secondary artist-page content fetched after first paint via
 *  /api/artist/{id}/extras. Split out because the Tidal calls behind
 *  it (the curated page + the radio mix) are the slow part of the
 *  artist page; the primary payload no longer blocks on them. */
export interface ArtistExtras {
  appears_on: Album[];
  compilations: Album[];
  artist_mix_id: string | null;
}

export interface ArtistDetail extends Artist {
  top_tracks: Track[];
  /** Mixed-format "Latest releases" row — the artist's albums, EPs,
   *  and singles newest-first (compilations and appears-on excluded).
   *  Computed server-side from the deduped discography. Capped at 12
   *  so the responsive single-row reflow always has more material
   *  than any breakpoint will show. */
  latest_releases: Album[];
  albums: Album[];
  ep_singles: Album[];
  /** The artist's own compilation releases (Greatest Hits, Best Of,
   *  anniversary collections) — split out of `albums` server-side by
   *  Tidal's album type so the Albums shelf is studio albums only. */
  compilations: Album[];
  appears_on: Album[];
  bio: string | null;
  similar: Artist[];
  share_url: string;
  /** Tidal's ARTIST_MIX id — the proper "Artist Radio" mix with
   *  composite cover. Null when no mix is available. */
  artist_mix_id: string | null;
  /** Music videos released by the artist. Empty when none. */
  videos: Video[];
  /** Tracks where the artist is credited in any role (producer,
   *  writer, featured, etc.). `role` annotated per row so the UI
   *  can group. Empty when unavailable for this artist / region. */
  credits: (Track & { role: string })[];
}

export interface MixDetail {
  kind: "mix";
  id: string;
  name: string;
  subtitle: string;
  cover: string | null;
  tracks: Track[];
}

export interface Lyrics {
  synced: { time: number; text: string }[] | null;
  text: string | null;
}

export interface CreditEntry {
  role: string;
  contributors: { name: string; id: string | null }[];
}

export interface PlaylistDetail extends Playlist {
  tracks: Track[];
}

export type DownloadStatus =
  | "Pending"
  | "Fetching…"
  | "Downloading"
  | "Tagging…"
  | "Complete"
  | "Failed";

export interface DownloadItem {
  id: string;
  title: string;
  artist: string;
  album: string;
  track_num: number;
  status: DownloadStatus;
  progress: number;
  error: string | null;
  file_path: string | null;
  /** Realtime throughput in bytes per second while the row is in
   *  Downloading state. 0 outside of that — Pending / Fetching /
   *  Tagging / Complete / Failed don't transfer bytes. Optional so
   *  older server builds that don't emit it just render the row
   *  without the speed annotation. */
  speed_bps?: number;
}

export interface VideoDownloadJob {
  video_id: number;
  /** "idle" when the server has no record, "running" while the
   *  remux is working, "done" after success, "error" after a
   *  failure. */
  state: "idle" | "running" | "done" | "error";
  title?: string;
  artist?: string;
  output_path?: string | null;
  error?: string | null;
  /** 0..1 while the remux is in flight. Null means "no progress
   *  yet" — not zero; the UI renders indeterminate rather than 0%
   *  when null. */
  progress?: number | null;
}

/** One band of the manual parametric EQ. `type` is a biquad filter
 *  code: "PK" (peaking), "LSC" (low shelf), or "HSC" (high shelf).
 *  Frequency in Hz, gain in dB, Q dimensionless. `enabled` lets the
 *  editor keep a band's settings while taking it out of the cascade. */
export interface ParametricBand {
  type: "PK" | "LSC" | "HSC";
  freq: number;
  gain: number;
  q: number;
  enabled: boolean;
}

/** Editable bounds + allowed filter types for the manual parametric
 *  EQ, served by GET /api/player/eq so the editor clamps to the same
 *  ranges the server validates against. */
export interface ManualEqConfig {
  filter_types: ("PK" | "LSC" | "HSC")[];
  freq_min: number;
  freq_max: number;
  gain_abs_max: number;
  q_min: number;
  q_max: number;
  max_bands: number;
}

export interface Settings {
  output_dir: string;
  /** Where music videos land. Kept separate from output_dir so video
   *  files don't intermix with album folders / iTunes-style music
   *  libraries. Default is ~/Movies/Tideway (macOS), ~/Videos/Tideway
   *  (Windows), or either on Linux depending on which exists. */
  videos_dir: string;
  filename_template: string;
  create_album_folders: boolean;
  /** When `create_album_folders` is on, prefix the album folder with
   *  the artist name so the layout is `<Artist> - <Album>/` instead of
   *  `<Album>/`. Off by default. */
  album_folder_includes_artist: boolean;
  skip_existing: boolean;
  concurrent_downloads: number;
  offline_mode: boolean;
  notify_on_complete: boolean;
  notify_on_track_change: boolean;
  /** How to handle Tidal returning both an explicit and a clean edit
   *  of the same album / track. "explicit" keeps the explicit copy
   *  (default), "clean" keeps the clean copy, "both" shows them both
   *  as Tidal returned. Mirrors the toggle Tidal's own client has
   *  under its "Explicit content" setting. */
  explicit_content_preference: "explicit" | "clean" | "both";
  /** Bit-perfect audio output — CoreAudio change_device_parameters
   *  + fail_if_conversion_required on macOS; WASAPI exclusive on
   *  Windows. No effect on Linux. */
  exclusive_mode: boolean;
  /** Pin software volume at 100 %; let the user attenuate via their
   *  DAC, speakers, or OS volume instead. Avoids software scaling
   *  that would otherwise throw away bit-depth under Exclusive Mode. */
  force_volume: boolean;
  /** Scroll-wheel volume step in percent (1..25). One wheel tick over
   *  the player bar's volume control moves the volume by this much;
   *  Shift+scroll always steps by 1 % for fine adjustment. */
  volume_scroll_step_pct: number;
  /** When the user's queue runs out — last track on an album,
   *  playlist, mix, single-track play, anything — take over with an
   *  Artist Radio mix seeded from the last track's primary artist.
   *  On by default to match Spotify / Apple Music "autoplay". Off
   *  falls back to per-source defaults (stop, or for albums prime
   *  track 0 paused so one tap repeats the album). */
  continue_playing_after_queue_ends: boolean;
  /** When another device on the same Tidal account starts playing,
   *  pause Tideway. Matches Spotify / official Tidal client behaviour.
   *  On by default. The realtime listener that drives this is wired
   *  up but the protocol capture is pending (see
   *  private/features/cross-device-pause-listener.md), so the toggle
   *  has no effect today regardless of value. */
  pause_on_other_device: boolean;
  /** Per-track download rate cap in MB/s. 0 = unlimited. Default 10
   *  so downloads look like aggressive prefetch rather than a scrape
   *  to Tidal's CDN — the single most effective ban-risk reduction
   *  lever on the client. */
  download_rate_limit_mbps: number;
  /** AutoEQ mode: "off" bypasses the EQ stage, "manual" uses
   *  `eq_bands` / `eq_preamp`, "profile" uses the bundled / cached
   *  AutoEQ profile named by `eq_active_profile_id`. The legacy
   *  `eq_enabled` flag is the master gate; this picks which path
   *  runs when enabled. Most state changes flow through the
   *  dedicated `/api/eq/*` endpoints rather than `PUT /api/settings`,
   *  but the field still has to mirror through the Pydantic
   *  payload so settings.json round-trips don't lose it. */
  eq_mode: "off" | "manual" | "profile";
  /** Currently-loaded AutoEQ profile id (e.g. `oratory1990/Sennheiser HD 600`).
   *  Empty when no profile is loaded. */
  eq_active_profile_id: string;
  /** A/B-bypass flag for the active EQ profile. Toggled from the
   *  signal-path strip in the FullScreenPlayer. Persisted across
   *  restart so the user's last comparison state is preserved. */
  eq_bypass: boolean;
  /** Output-device fingerprint → profile_id map for automatic
   *  profile switching when the user plugs in a different DAC.
   *  null value means "explicitly mapped to no profile" (i.e.
   *  EQ off for this device); missing key means "no mapping yet". */
  eq_device_mappings: Record<string, string | null>;
  /** Behaviour when an output device has no mapping in
   *  `eq_device_mappings`. "bypass" runs the audio without EQ;
   *  "use_last_profile" keeps whatever profile was last active. */
  eq_fallback_when_unmapped: "bypass" | "use_last_profile";
  /** User taste-layer adjustment stacked on top of the profile
   *  correction. Master preamp added to the profile's recommended
   *  preamp; UI clamps to ±12 dB. */
  eq_tilt_preamp_offset_db: number;
  /** Low-shelf boost / cut at 80 Hz, layered after the profile bands. */
  eq_tilt_bass_db: number;
  /** High-shelf boost / cut at 8 kHz, layered after the profile bands. */
  eq_tilt_treble_db: number;
  /** Bauer-style crossfeed strength as a percent (0-100). 0 disables;
   *  20-40 is the typical taste range for hard-panned mixes on
   *  headphones. */
  crossfeed_amount: number;
  /** ReplayGain loudness leveling mode. "off" preserves bit-perfect
   *  output; "track" applies the per-track gain (best for shuffle);
   *  "album" applies the album-wide gain (best for whole-album
   *  listening). */
  replaygain_mode: "off" | "track" | "album";
  /** User offset in dB applied on top of the ReplayGain value
   *  (-10..+10). Useful when EBU R128 reference is too quiet for a
   *  particular DAC + headphone combination. */
  replaygain_preamp_db: number;
  /** When true, the gain is clamped so peak * gain ≤ 1.0 to prevent
   *  clipping on quieter masters with negative gain offsets. */
  replaygain_prevent_clipping: boolean;
  /** Last software volume (0..100), persisted so a restart doesn't
   *  jump back to 100 %. */
  volume: number;
  /** Group a downloaded playlist's tracks under a folder named after
   *  the playlist; {playlist_num} then numbers them in playlist
   *  order. */
  create_playlist_folders: boolean;
  /** Downconvert hi-res (24-bit / >48 kHz) downloads to 16-bit /
   *  44.1 kHz FLAC for legacy DAPs (old iPod + Rockbox, etc.).
   *  Off by default; CD-quality and lossy sources are untouched. */
  downconvert_hires_downloads: boolean;
  /** Resolution of album cover art embedded in downloads and written
   *  as cover.jpg. "640" / "1280" / "origin" (3000x3000 master).
   *  Default "1280". */
  cover_art_resolution: "640" | "1280" | "origin";
  /** Desktop window geometry, persisted on close. -1 = not set yet
   *  (first run uses the platform default). */
  window_x: number;
  window_y: number;
  window_width: number;
  window_height: number;
}

/** Snapshot of every stage in the audio DSP chain — drives the
 *  "Signal path" panel users open from the now-playing pill to
 *  confirm what's actually happening to the bits. */
export interface SignalPath {
  /** True only when a track is loaded AND no DSP stage is active
   *  AND exclusive mode is on AND no external output (Tidal
   *  Connect / DLNA receiver) is taking over. */
  bit_perfect: boolean;
  /** Whether there's actually a track in the audio pipeline right
   *  now. Drives the idle / informational state of the panel. */
  track_loaded: boolean;
  source: {
    codec: string | null;
    sample_rate_hz: number | null;
    bit_depth: number | null;
    audio_quality: string | null;
  };
  replaygain: {
    mode: "off" | "track" | "album";
    applied_db: number;
    preamp_db: number;
    prevent_clipping: boolean;
    tags_present: boolean;
    active: boolean;
  };
  eq: {
    mode: "off" | "manual" | "profile";
    bypass: boolean;
    profile_id: string | null;
    manual_enabled: boolean;
    active: boolean;
  };
  crossfeed: {
    amount: number;
    active: boolean;
  };
  output: {
    exclusive_mode: boolean;
    force_volume: boolean;
    device_name: string | null;
    sample_rate_hz: number | null;
    bit_depth: number | null;
    channels: number | null;
    sd_dtype: string | null;
    /** True when a Tidal Connect / DLNA receiver is the active
     *  sink — local output is silenced and the DSP stages below
     *  don't run on the remote audio (the receiver gets pre-DSP
     *  PCM). */
    external_output_active: boolean;
  };
}

export interface AuthStatus {
  logged_in: boolean;
  username: string | null;
  avatar: string | null;
  user_id: string | null;
}

/** Tidal subscription tiers Tideway maps `get_max_quality()` to. The
 *  download buttons gate on `can_download`, derived from this. */
export type SubscriptionTier = "max" | "lossless" | "lossy" | "unknown";

export interface SubscriptionStatus {
  tier: SubscriptionTier;
  can_download: boolean;
  reason: string | null;
}

export interface LastFmStatus {
  /** True once the user has pasted API key + secret from last.fm,
   *  OR the build ships with baked-in default credentials. */
  has_credentials: boolean;
  /** True when the app is running with module-level default credentials
   *  and the user hasn't overridden them. Used to hide the "Reset
   *  credentials" affordance — there's nothing personal to reset. */
  using_default_credentials: boolean;
  /** True once the user has finished the browser auth flow — scrobbles
   *  and now-playing will actually go through. */
  connected: boolean;
  username: string | null;
}

/** One entry from Last.fm's `user.getRecentTracks`. `played_at` is a
 *  UNIX epoch in seconds, null when `now_playing` is true. */
export interface LastFmRecentTrack {
  artist: string;
  track: string;
  album: string;
  played_at: number | null;
  now_playing: boolean;
  cover: string;
  url: string;
}

export type LastFmPeriod =
  | "overall"
  | "7day"
  | "1month"
  | "3month"
  | "6month"
  | "12month";

export interface LastFmUserInfo {
  username: string;
  realname: string;
  playcount: number;
  track_count: number;
  artist_count: number;
  album_count: number;
  country: string;
  url: string;
  registered_at: number | null;
  image: string;
}

export interface LastFmTopArtist {
  name: string;
  playcount: number;
  url: string;
  image: string;
  mbid: string;
}

export interface LastFmTopTrack {
  name: string;
  artist: string;
  playcount: number;
  duration: number;
  url: string;
  image: string;
}

export interface LastFmTopAlbum {
  name: string;
  artist: string;
  playcount: number;
  url: string;
  image: string;
}

/** One row from an AlbumOfTheYear listing endpoint. The AOTY metadata
 *  describes the album as AOTY presents it (rank, score, cover URL,
 *  release date), and `tidal_album` is the resolved Tidal album dict
 *  when AOTY's pick exists on Tidal — null when it doesn't, in which
 *  case the row is shown but isn't directly playable. */
export interface AotyAlbum {
  title: string;
  artist: string;
  score: number | null;
  rating_count: number | null;
  cover_url: string | null;
  release_date: string | null;
  rank: number | null;
  must_hear: boolean;
  aoty_url: string | null;
  /** AOTY's genre tags. Populated for the Top-of-year list rows;
   *  the this-week New-releases cards don't carry genre, so those
   *  come back empty. `genre_slugs` is parallel to `genres` (same
   *  order/length) — the "{id}-{slug}" segment for fetching that
   *  genre's chart. */
  genres: string[];
  genre_slugs: string[];
  tidal_album: Album | null;
}

/** One entry from AOTY's genre index — `slug` is AOTY's
 *  "{id}-{kebab-name}" path segment, `name` the display label. */
export interface AotyGenre {
  slug: string;
  name: string;
}

export interface LastFmLovedTrack {
  name: string;
  artist: string;
  loved_at: number | null;
  url: string;
  image: string;
}

export interface LastFmPlaycount {
  /** How many times THIS user has played this entity. Missing / 0
   *  when the user has never scrobbled it. */
  userplaycount?: number;
  /** Only on track.getInfo: 1 if the user has loved it. */
  userloved?: boolean;
  /** Global stats for the entity across all Last.fm users. */
  listeners?: number;
  playcount?: number;
  url?: string;
}

export interface LastFmWeeklyScrobble {
  /** UNIX epoch (seconds) for the start / end of the 7-day bucket. */
  from: number;
  to: number;
  count: number;
}

export interface LastFmChartArtist {
  name: string;
  playcount: number;
  listeners: number;
  url: string;
  image: string;
  mbid: string;
}

export interface LastFmChartTrack {
  name: string;
  artist: string;
  playcount: number;
  listeners: number;
  duration: number;
  url: string;
  image: string;
}

export interface LastFmChartTag {
  name: string;
  taggings: number;
  reach: number;
  url: string;
}

export interface QualityOption {
  value: string;
  label: string;
  codec: string;
  bitrate: string;
  description: string;
}

export type FavoriteKind = "track" | "album" | "artist" | "playlist" | "mix";

export interface FavoritesSnapshot {
  tracks: string[];
  albums: string[];
  artists: string[];
  playlists: string[];
  mixes: string[];
}

export interface MixItem {
  kind: "mix";
  id: string;
  name: string;
  subtitle: string;
  cover: string | null;
}

export interface PageLinkItem {
  kind: "pagelink";
  title: string;
  path: string;
  icon: string;
}

export type PageItem =
  | Track
  | Album
  | Artist
  | Playlist
  | MixItem
  | PageLinkItem;

/** Clickable entity reference for category headers like
 *  "Because you liked X" — lets the UI show a thumbnail next to the
 *  section title that navigates to the referenced album/artist/etc. */
export interface PageContext {
  kind: "album" | "artist" | "playlist" | "mix" | "track";
  id: string;
  title: string;
  cover: string | null;
}

export interface PageCategory {
  type: string; // HorizontalList, TrackList, PageLinks, ShortcutList, etc.
  title: string;
  /** Secondary label from Tidal — e.g. the track/artist name following
   *  "Because you liked" or "Because you listened to". Only present when
   *  it adds information beyond `title`. */
  subtitle?: string;
  /** Present on HORIZONTAL_LIST_WITH_CONTEXT rows — the related entity
   *  that motivated the recommendation. */
  context?: PageContext;
  /** Present when Tidal offers a dedicated "view all" page for this row. */
  viewAllPath?: string;
  items: PageItem[];
}

export interface TidalPage {
  title?: string;
  categories: PageCategory[];
}

export interface LocalFile {
  path: string;
  relative_path: string;
  title: string;
  artist: string;
  album: string;
  /** Canonical album-level artist (FLAC `albumartist` / MP4 `aART`).
   *  Older downloads predate this tag, so it can be null; the UI
   *  falls back to deriving a primary artist from the per-track
   *  `artist` field when this is missing. */
  album_artist: string | null;
  track_num: number;
  tidal_id: string | null;
  duration: number;
  size_bytes: number;
  ext: string;
  /** File mtime in seconds since epoch — drives the "Recent" sort
   *  on the On-This-Device page. */
  mtime: number;
}

/** A video file the user has downloaded. No album / track_num / tidal_id
 *  fields because the remux doesn't author tags; metadata is parsed
 *  from the `<Artist> - <Title>` filename the downloader writes. */
export interface LocalVideo {
  path: string;
  relative_path: string;
  title: string;
  artist: string;
  size_bytes: number;
  ext: string;
  mtime: number;
}
