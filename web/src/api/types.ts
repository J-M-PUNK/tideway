export type ContentKind = "track" | "album" | "artist" | "playlist";

export interface ArtistRef {
  id: string;
  name: string;
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
}

export interface Artist {
  kind: "artist";
  id: string;
  name: string;
  picture: string | null;
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
  owned: boolean;
}

export type LibraryItem = Track | Album | Artist | Playlist;

export interface SearchResponse {
  tracks: Track[];
  albums: Album[];
  artists: Artist[];
  playlists: Playlist[];
}

export interface AlbumDetail extends Album {
  tracks: Track[];
  similar: Album[];
  review: string | null;
}

export interface ArtistDetail extends Artist {
  top_tracks: Track[];
  albums: Album[];
  ep_singles: Album[];
  appears_on: Album[];
  bio: string | null;
  similar: Artist[];
  share_url: string;
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
}

export interface Settings {
  output_dir: string;
  quality: string;
  filename_template: string;
  create_album_folders: boolean;
  skip_existing: boolean;
  concurrent_downloads: number;
}

export interface AuthStatus {
  logged_in: boolean;
  username: string | null;
  avatar: string | null;
}

export interface QualityOption {
  value: string;
  label: string;
  codec: string;
  bitrate: string;
  description: string;
}

export type FavoriteKind = "track" | "album" | "artist" | "playlist";

export interface FavoritesSnapshot {
  tracks: string[];
  albums: string[];
  artists: string[];
  playlists: string[];
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

export type PageItem = Track | Album | Artist | Playlist | MixItem | PageLinkItem;

export interface PageCategory {
  type: string; // HorizontalList, TrackList, PageLinks, ShortcutList, etc.
  title: string;
  /** Secondary label from Tidal — e.g. the track/artist name following
   *  "Because you liked" or "Because you listened to". Only present when
   *  it adds information beyond `title`. */
  subtitle?: string;
  items: PageItem[];
}

export interface TidalPage {
  categories: PageCategory[];
}
