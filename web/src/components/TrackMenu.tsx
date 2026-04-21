import type { ComponentType, ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  CheckSquare,
  Disc3,
  Download,
  ExternalLink,
  FileText,
  Heart,
  Link as LinkIcon,
  ListPlus,
  Music,
  Play,
  Plus,
  Radio,
  Share2,
  Trash2,
  User,
} from "lucide-react";
import { api } from "@/api/client";
import type { Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { useFavorites } from "@/hooks/useFavorites";
import { useMyPlaylists } from "@/hooks/useMyPlaylists";
import { useTrackSelection } from "@/hooks/useTrackSelection";
import { useQualities } from "@/hooks/useQualities";
import { useToast } from "@/components/toast";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { cn, imageProxy } from "@/lib/utils";
import {
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
} from "@/components/ui/context-menu";
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * A set of menu primitives — Item / Separator / Sub / SubTrigger /
 * SubContent. Radix ships two distinct primitive families for right-click
 * (ContextMenu, positions at cursor) and click (DropdownMenu, positions
 * relative to a trigger). They share an identical structural API at the
 * Item level but register with different providers, so you can't hot-swap
 * one for the other. Callers hand a MenuParts bundle to TrackMenuItems
 * so a single render tree can power both the right-click menu on each
 * track row AND the three-dots button / Now-Playing right-click menu
 * without duplicating ~150 lines of JSX.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyComp = ComponentType<any>;

export interface MenuParts {
  Item: AnyComp;
  Separator: AnyComp;
  Sub: AnyComp;
  SubTrigger: AnyComp;
  SubContent: AnyComp;
}

export const CONTEXT_MENU_PARTS: MenuParts = {
  Item: ContextMenuItem,
  Separator: ContextMenuSeparator,
  Sub: ContextMenuSub,
  SubTrigger: ContextMenuSubTrigger,
  SubContent: ContextMenuSubContent,
};

export const DROPDOWN_MENU_PARTS: MenuParts = {
  Item: DropdownMenuItem,
  Separator: DropdownMenuSeparator,
  Sub: DropdownMenuSub,
  SubTrigger: DropdownMenuSubTrigger,
  SubContent: DropdownMenuSubContent,
};

export interface TrackMenuItemsProps {
  /** Which primitive family to render — ContextMenu or DropdownMenu. */
  parts: MenuParts;
  track: Track;
  /** Surrounding track list to use as the playback queue when "Play" is
   *  selected. Defaults to just the track itself. */
  context?: Track[];
  onDownload: OnDownload;
  /** When supplied, renders a "Remove from playlist" item at the bottom. */
  onRemove?: () => void;
  /** Opens the credits dialog. Parent owns the dialog state so its
   *  lifecycle isn't tied to the menu being open. */
  onShowCredits: () => void;
  /** Multi-select only makes sense inside a list that can hold a
   *  selection set (TrackList). Callers rendering from outside that
   *  context (e.g. the now-playing bar's three-dots menu) pass false. */
  showSelect?: boolean;
}

/**
 * Shared menu-body renderer. Drop it inside a `<ContextMenuContent>` or
 * `<DropdownMenuContent>` with the matching `parts` bundle; you'll get
 * identical behavior whether the menu was summoned via right-click or a
 * three-dots button. Does not render the Content wrapper itself —
 * callers control positioning and portal behavior.
 */
export function TrackMenuItems({
  parts,
  track,
  context,
  onDownload,
  onRemove,
  onShowCredits,
  showSelect = true,
}: TrackMenuItemsProps): ReactNode {
  const { Item, Separator } = parts;
  const toast = useToast();
  const actions = usePlayerActions();
  const favs = useFavorites();
  const selection = useTrackSelection();
  const liked = favs.has("track", track.id);
  const isSelected = selection.has(track.id);
  // Hide "Go to album" / "Go to artist" when we're already on that
  // destination — the menu item would route to the current page and
  // do nothing, which is noise. Match by pathname since useParams
  // isn't available when TrackMenu is rendered outside a Route.
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const onAlbumPage =
    !!track.album && pathname === `/album/${encodeURIComponent(track.album.id)}`;
  const onArtistPage =
    !!track.artists[0] &&
    pathname === `/artist/${encodeURIComponent(track.artists[0].id)}`;

  const playQueue = context && context.length ? context : [track];

  const startRadio = () => {
    // Prefer Tidal's canonical TRACK_MIX page — it comes with the
    // composite cover, "Track Radio" subtitle, and any other entities
    // Tidal renders on a real mix. Fall back to our generic radio
    // page when Tidal hasn't minted a mix for this track.
    if (track.track_mix_id) {
      navigate(`/mix/${encodeURIComponent(track.track_mix_id)}`);
      return;
    }
    const seed = {
      name: track.name,
      cover: track.album?.cover ?? null,
    };
    navigate(`/radio/track/${track.id}`, { state: { seed } });
  };

  const shareUrl = track.share_url || `https://tidal.com/browse/track/${track.id}`;

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      toast.show({ kind: "success", title: "Link copied", description: shareUrl });
    } catch {
      toast.show({
        kind: "error",
        title: "Copy failed",
        description: "Clipboard not available.",
      });
    }
  };

  const openOnTidal = async () => {
    try {
      await api.openExternal(shareUrl);
    } catch {
      window.open(shareUrl, "_blank", "noopener");
    }
  };

  return (
    <>
      <TrackHeader parts={parts} track={track} />
      <Item onSelect={() => actions.play(track, playQueue)}>
        <Play className="h-3.5 w-3.5" /> Play
      </Item>
      <Item onSelect={() => actions.playNext(track)}>
        <ListPlus className="h-3.5 w-3.5" /> Play next
      </Item>
      <AddToPlaylistSubmenu parts={parts} trackId={track.id} trackName={track.name} />
      <Item onSelect={() => favs.toggle("track", track.id)}>
        <Heart
          className={cn("h-3.5 w-3.5", liked && "fill-primary text-primary")}
        />
        {liked ? "Remove" : "Add"}
      </Item>
      <Item onSelect={startRadio}>
        <Radio className="h-3.5 w-3.5" /> Go to track radio
      </Item>
      <DownloadSubmenu
        parts={parts}
        onPick={(quality) => onDownload("track", track.id, quality)}
        mediaTags={track.media_tags}
      />
      {track.album && !onAlbumPage && (
        <Item asChild>
          <Link to={`/album/${track.album.id}`}>
            <Disc3 className="h-3.5 w-3.5" /> Go to album
          </Link>
        </Item>
      )}
      {track.artists[0] && !onArtistPage && (
        <Item asChild>
          <Link to={`/artist/${track.artists[0].id}`}>
            <User className="h-3.5 w-3.5" /> Go to artist
          </Link>
        </Item>
      )}
      <Item onSelect={onShowCredits}>
        <FileText className="h-3.5 w-3.5" /> Credits…
      </Item>
      <ShareSubmenu parts={parts} onCopy={copyLink} onOpen={openOnTidal} />
      {showSelect && (
        <Item onSelect={() => selection.toggle(track)}>
          <CheckSquare className={cn("h-3.5 w-3.5", isSelected && "text-primary")} />
          {isSelected ? "Deselect" : "Select"}
        </Item>
      )}
      {onRemove && (
        <>
          <Separator />
          <Item onSelect={onRemove} className="text-destructive">
            <Trash2 className="h-3.5 w-3.5" /> Remove from playlist
          </Item>
        </>
      )}
    </>
  );
}

/**
 * Tidal-style header at the top of the track menu — small cover art +
 * track name + primary artist. Purely informational; not an `<Item>`
 * so it doesn't interfere with keyboard navigation through the menu.
 */
function TrackHeader({ parts, track }: { parts: MenuParts; track: Track }) {
  const { Separator } = parts;
  const cover = track.album?.cover ? imageProxy(track.album.cover) : undefined;
  const artist = track.artists.map((a) => a.name).join(", ");
  return (
    <>
      <div className="flex items-center gap-3 px-3 py-2">
        <div className="h-10 w-10 flex-shrink-0 overflow-hidden rounded bg-secondary">
          {cover ? (
            <img src={cover} alt="" className="h-full w-full object-cover" />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Music className="h-4 w-4" />
            </div>
          )}
        </div>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{track.name}</div>
          <div className="truncate text-xs text-muted-foreground">{artist}</div>
        </div>
      </div>
      <Separator />
    </>
  );
}

function ShareSubmenu({
  parts,
  onCopy,
  onOpen,
}: {
  parts: MenuParts;
  onCopy: () => void;
  onOpen: () => void;
}) {
  const { Item, Sub, SubTrigger, SubContent } = parts;
  return (
    <Sub>
      <SubTrigger>
        <Share2 className="h-3.5 w-3.5" /> Share
      </SubTrigger>
      <SubContent>
        <Item onSelect={onCopy}>
          <LinkIcon className="h-3.5 w-3.5" /> Copy Tidal link
        </Item>
        <Item onSelect={onOpen}>
          <ExternalLink className="h-3.5 w-3.5" /> Open on Tidal
        </Item>
      </SubContent>
    </Sub>
  );
}


function AddToPlaylistSubmenu({
  parts,
  trackId,
  trackName,
}: {
  parts: MenuParts;
  trackId: string;
  trackName: string;
}) {
  const { Item, Separator, Sub, SubTrigger, SubContent } = parts;
  const { playlists } = useMyPlaylists();
  const toast = useToast();

  const add = async (playlistId: string, playlistName: string) => {
    try {
      await api.playlists.addTracks(playlistId, [trackId]);
      toast.show({
        kind: "success",
        title: "Added to playlist",
        description: `"${trackName}" → ${playlistName}`,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't add to playlist",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <Sub>
      <SubTrigger>
        <Plus className="h-3.5 w-3.5" /> Add to playlist…
      </SubTrigger>
      <SubContent className="max-h-96 overflow-y-auto">
        <CreatePlaylistDialog
          trigger={
            <button className="flex w-full cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent">
              <Plus className="h-3.5 w-3.5" /> New playlist…
            </button>
          }
        />
        {playlists.length > 0 && <Separator />}
        {playlists.map((p) => (
          <Item key={p.id} onSelect={() => add(p.id, p.name)}>
            <span className="truncate">{p.name}</span>
          </Item>
        ))}
        {playlists.length === 0 && (
          <div className="px-3 py-2 text-xs text-muted-foreground">
            No playlists yet. Create one above.
          </div>
        )}
      </SubContent>
    </Sub>
  );
}

function DownloadSubmenu({
  parts,
  onPick,
  mediaTags,
}: {
  parts: MenuParts;
  onPick: (quality?: string) => void;
  mediaTags?: string[];
}) {
  const { Item, Separator, Sub, SubTrigger, SubContent } = parts;
  const qualities = useQualities() ?? [];
  return (
    <Sub>
      <SubTrigger>
        <Download className="h-3.5 w-3.5" /> Download…
      </SubTrigger>
      <SubContent>
        <Item onSelect={() => onPick()}>Use default quality</Item>
        <Separator />
        {qualities.map((q) => {
          const effective = trackEffectiveFormat(q.value, mediaTags);
          return (
            <Item key={q.value} onSelect={() => onPick(q.value)}>
              <div className="flex flex-col">
                <div className="flex items-center gap-2">
                  <span>
                    {q.label} · {q.codec}
                  </span>
                  {effective && (
                    <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
                      {effective}
                    </span>
                  )}
                </div>
                <span className="text-[11px] text-muted-foreground">{q.bitrate}</span>
              </div>
            </Item>
          );
        })}
      </SubContent>
    </Sub>
  );
}

/**
 * Same logic as DownloadButton's effectiveFormatLabel. Duplicated
 * (rather than imported) because the TrackMenu's Radix submenu
 * primitives don't mount children eagerly — pulling in the full
 * DownloadButton for its helper would be overkill.
 */
function trackEffectiveFormat(
  quality: string,
  tags: string[] | undefined,
): string | null {
  // Keep in sync with DownloadButton.effectiveFormatLabel. Only Max
  // gets annotated; only tracks that actually ship as hi-res FLAC
  // benefit from it. Immersive-audio tags aren't surfaced — see the
  // note on the DownloadButton helper.
  if (quality !== "hi_res_lossless") return null;
  if (!tags || tags.length === 0) return null;
  const T = new Set(tags.map((x) => x.toUpperCase()));
  if (T.has("HIRES_LOSSLESS")) return "Hi-Res FLAC";
  if (T.has("LOSSLESS")) return "Same as Lossless";
  return null;
}
