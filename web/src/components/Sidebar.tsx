import { NavLink } from "react-router-dom";
import {
  BarChart3,
  Disc3,
  Download,
  HardDrive,
  Heart,
  History,
  Home,
  Import as ImportIcon,
  Layers,
  Library,
  Link2,
  ListMusic,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Rss,
  TrendingUp,
  User,
  X,
} from "lucide-react";
import { AddUrlDialog } from "@/components/AddUrlDialog";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { useFeedUnreadCount } from "@/hooks/useFeedUnread";
import { useUiPreferences } from "@/hooks/useUiPreferences";
import { cn } from "@/lib/utils";
import { prefetch } from "@/api/queryKeys";

// The Popular page (Last.fm-backed global charts) is the discovery
// entry. Tidal's editorial Top / Rising tabs were removed as
// redundant; New Releases lives on its own route reached from Home.
//
// `prefetch` fires on hover so the page's data is already in the
// useApi cache by the time the click lands. No-op when the cache
// is already fresh — see web/src/hooks/useApi.ts.
const primary: Array<{
  to: string;
  label: string;
  icon: typeof Home;
  end?: boolean;
  prefetch?: () => void;
}> = [
  {
    to: "/",
    label: "Home",
    icon: Home,
    end: true,
    prefetch: prefetch.pageHome,
  },
  { to: "/feed", label: "Feed", icon: Rss, prefetch: prefetch.feed },
  {
    to: "/popular",
    label: "Popular",
    icon: TrendingUp,
    prefetch: prefetch.popularArtists,
  },
];

// Library order: the things the user curates (Liked Songs → Albums →
// Artists → Playlists) come first, then derived/supporting surfaces
// (local files, history, stats). `ListMusic` — not `Library` — for
// Playlists because the section header already uses the `Library`
// icon; two rows with the same glyph looks like a bug at a glance.
const library: Array<{
  to: string;
  label: string;
  icon: typeof Home;
  prefetch?: () => void;
}> = [
  {
    to: "/library/tracks",
    label: "Liked Songs",
    icon: Heart,
    prefetch: prefetch.libraryTracks,
  },
  {
    to: "/library/albums",
    label: "Albums",
    icon: Disc3,
    prefetch: prefetch.libraryAlbums,
  },
  {
    to: "/library/artists",
    label: "Artists",
    icon: User,
    prefetch: prefetch.libraryArtists,
  },
  {
    to: "/library/playlists",
    label: "Playlists",
    icon: ListMusic,
    prefetch: prefetch.libraryPlaylists,
  },
  { to: "/library/collections", label: "Collections", icon: Layers },
  { to: "/library/local", label: "On this device", icon: HardDrive },
  { to: "/history", label: "History", icon: History },
  { to: "/stats", label: "Stats", icon: BarChart3 },
];

// In offline mode the only link we keep in the "Your Library" section
// is the local file list — everything else is Tidal-session-dependent.
const offlineLibrary: typeof library = [
  { to: "/library/local", label: "On this device", icon: HardDrive },
];

// Shared NavLink classes. The `before:` pseudo-element is the active
// indicator — a 2px primary-colored bar pinned to the left edge of
// the active row. Inactive rows have it at h-0 (invisible); the
// active class flips it to h-5 (matching the icon height) with a
// 200 ms transition so clicking between tabs reads as the bar
// growing into the new row rather than snapping. Same idea every
// modern sidebar uses (Spotify, Linear, Notion).
// `collapsed` swaps the row from icon+label (gap-4, left-aligned) to a
// centered icon-only tile so the same NavLink works in both sidebar
// widths without duplicating markup.
const navItemClass = (isActive: boolean, collapsed = false) =>
  cn(
    "relative flex items-center rounded-md py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground",
    collapsed ? "justify-center px-2" : "gap-4 px-3",
    "before:absolute before:left-0 before:top-1/2 before:w-0.5 before:-translate-y-1/2 before:rounded-r before:bg-primary before:transition-all before:duration-200",
    isActive
      ? "text-foreground before:h-5 before:opacity-100"
      : "before:h-0 before:opacity-0",
  );

export function Sidebar({
  activeDownloads,
  newDownloads,
  offline = false,
}: {
  activeDownloads: number;
  /** Count of completed downloads the user hasn't looked at yet. Shown
   *  only when no downloads are currently active — otherwise the active
   *  count takes precedence. */
  newDownloads: number;
  /** When true, hide everything that needs a live Tidal session — the
   *  user is signed out but has offline mode enabled. */
  offline?: boolean;
}) {
  const libraryLinks = offline ? offlineLibrary : library;
  const feedUnread = useFeedUnreadCount();
  const {
    importLinkDismissed,
    sidebarCollapsed: collapsed,
    set: setPrefs,
  } = useUiPreferences();
  return (
    <aside
      className={cn(
        "flex h-full flex-col gap-2 bg-background p-2 text-sm transition-[width] duration-200 ease-out",
        collapsed ? "w-16" : "w-64",
      )}
    >
      {/* Brand mark at the top left of the app. It also works as a
          Home link, which matters in offline mode where the normal
          Home nav row further down is hidden.

          The NavLink is sized to exactly the 40 pixel glyph, so the
          click target matches what the user actually sees and there
          is no invisible hit area bleeding across the sidebar. The
          `ml-[10px]` offset lines the icon center up with the same
          vertical column the smaller nav icons below sit on. Math:
          8 pixels of aside padding, plus 10 pixels of margin, plus
          half of the 40 pixel glyph lands at 38, which is where the
          20 pixel nav icons have their centers.

          No hover or active background tile — the icon itself is
          transparent and sits directly on the sidebar's dark
          background. A subtle scale bump on hover still makes it
          feel interactive without painting a box behind the glyph. */}
      <div
        className={cn(
          "flex",
          collapsed
            ? "flex-col items-center gap-1"
            : "items-center justify-between pr-1",
        )}
      >
        <NavLink
          to="/"
          end
          aria-label="Home"
          title="Home"
          onMouseEnter={prefetch.pageHome}
          className={cn(
            "inline-flex h-10 w-10 items-center justify-center transition-transform duration-150 hover:scale-105",
            // The 10px nudge lines the 40px brand glyph up with the 20px
            // nav icons below in the expanded rail; collapsed, everything
            // is centered so the offset would push it off-center instead.
            !collapsed && "ml-[10px]",
          )}
        >
          <img src="/app-icon.svg" alt="" className="h-10 w-10 shrink-0" />
        </NavLink>
        <button
          type="button"
          onClick={() => setPrefs({ sidebarCollapsed: !collapsed })}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-pressed={collapsed}
          className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          {collapsed ? (
            <PanelLeftOpen className="h-5 w-5" />
          ) : (
            <PanelLeftClose className="h-5 w-5" />
          )}
        </button>
      </div>

      {!offline && (
        <nav className="rounded-lg bg-card p-2">
          {primary.map(({ to, label, icon: Icon, end, prefetch: warm }) => {
            const badge = to === "/feed" && feedUnread > 0 ? feedUnread : 0;
            return (
              <NavLink
                key={to}
                to={to}
                end={end}
                onMouseEnter={warm}
                title={collapsed ? label : undefined}
                className={({ isActive }) => navItemClass(isActive, collapsed)}
              >
                <span className="relative flex shrink-0">
                  <Icon className="h-5 w-5" />
                  {/* Collapsed rows have no room for the count pill, so a
                      dot on the icon still signals "there's something new"
                      without the number. */}
                  {collapsed && badge > 0 && (
                    <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-primary" />
                  )}
                </span>
                {!collapsed && <span className="flex-1">{label}</span>}
                {!collapsed && badge > 0 && (
                  <span
                    className="rounded-full bg-primary/20 px-2 py-0.5 text-[10px] font-bold text-primary"
                    title={`${badge} new release${badge === 1 ? "" : "s"}`}
                  >
                    {badge > 99 ? "99+" : badge}
                  </span>
                )}
              </NavLink>
            );
          })}
        </nav>
      )}

      <div className="flex min-h-0 flex-1 flex-col rounded-lg bg-card p-2">
        <div
          className={cn(
            "flex items-center py-2 text-muted-foreground",
            collapsed ? "justify-center px-0" : "justify-between px-3",
          )}
        >
          <span
            className={cn(
              "flex items-center text-sm font-semibold",
              !collapsed && "gap-3",
            )}
            title={collapsed ? "Your Library" : undefined}
          >
            <Library className="h-5 w-5" /> {!collapsed && "Your Library"}
          </span>
          {!offline && !collapsed && (
            <CreatePlaylistDialog
              trigger={
                <button
                  // A bit bigger + filled-on-hover so the primary action
                  // (create a playlist) reads as an action, not a
                  // decoration. Tooltip hints at what "+" means since
                  // the icon alone is ambiguous next to a library label.
                  className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                  title="New playlist"
                  aria-label="Create a new playlist"
                >
                  <Plus className="h-4 w-4" />
                </button>
              }
            />
          )}
        </div>
        <div className="mt-1 flex flex-col gap-px overflow-y-auto scrollbar-thin">
          {libraryLinks.map(({ to, label, icon: Icon, prefetch: warm }) => (
            <NavLink
              key={to}
              to={to}
              onMouseEnter={warm}
              title={collapsed ? label : undefined}
              className={({ isActive }) =>
                cn(
                  navItemClass(isActive, collapsed),
                  // Library rows additionally fade the background on
                  // hover so this section reads as a more
                  // interaction-heavy surface than the primary /
                  // discover blocks above. Keeps the user's
                  // most-frequented area feeling distinct.
                  "hover:bg-accent",
                )
              }
            >
              <Icon className="h-5 w-5 shrink-0" />
              {!collapsed && label}
            </NavLink>
          ))}
          {!offline && !importLinkDismissed && collapsed && (
            <NavLink
              to="/import"
              title="Import"
              className={({ isActive }) =>
                cn(navItemClass(isActive, true), "hover:bg-accent")
              }
            >
              <ImportIcon className="h-5 w-5 shrink-0" />
            </NavLink>
          )}
          {!offline && !importLinkDismissed && !collapsed && (
            <div className="group relative">
              <NavLink
                to="/import"
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-4 rounded-md px-3 py-2 pr-9 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                    isActive && "bg-accent text-foreground",
                  )
                }
              >
                <ImportIcon className="h-5 w-5" />
                <span className="flex-1">Import</span>
              </NavLink>
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setPrefs({ importLinkDismissed: true });
                }}
                title="Hide from sidebar — you can always reopen from Settings"
                aria-label="Hide Import from sidebar"
                className="absolute right-1.5 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground opacity-0 transition-opacity hover:bg-background hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </div>
      </div>

      <div
        className={cn("flex gap-2", collapsed ? "flex-col" : "items-center")}
      >
        <NavLink
          to="/downloads"
          title={collapsed ? "Downloads" : undefined}
          className={({ isActive }) =>
            cn(
              "flex items-center rounded-lg bg-card text-sm font-semibold text-muted-foreground hover:text-foreground",
              collapsed
                ? "w-full justify-center px-2 py-2"
                : "flex-1 gap-3 px-3 py-2",
              isActive && "text-foreground",
            )
          }
        >
          <div className="relative">
            <Download className="h-4 w-4" />
            {(activeDownloads > 0 || newDownloads > 0) && (
              <span className="absolute -right-1 -top-1 flex h-2 w-2">
                {activeDownloads > 0 && (
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-75" />
                )}
                <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
              </span>
            )}
          </div>
          {!collapsed && "Downloads"}
          {!collapsed &&
            (activeDownloads > 0 ? (
              <span className="ml-auto rounded-full bg-primary/20 px-2 py-0.5 text-[10px] font-bold text-primary">
                {activeDownloads}
              </span>
            ) : newDownloads > 0 ? (
              <span className="ml-auto rounded-full bg-primary/20 px-2 py-0.5 text-[10px] font-bold text-primary">
                {newDownloads}
              </span>
            ) : null)}
        </NavLink>
        {!collapsed && (
          <AddUrlDialog
            trigger={
              <button
                className="flex h-9 w-9 items-center justify-center rounded-lg bg-card text-muted-foreground hover:text-foreground"
                title="Download from Tidal URL"
              >
                <Link2 className="h-4 w-4" />
              </button>
            }
          />
        )}
      </div>
    </aside>
  );
}
