import { NavLink } from "react-router-dom";
import {
  BarChart3,
  Compass,
  Disc3,
  Download,
  HardDrive,
  Heart,
  History,
  Home,
  Import as ImportIcon,
  Library,
  Link2,
  ListMusic,
  Newspaper,
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

const primary = [
  { to: "/", label: "Home", icon: Home, end: true },
  { to: "/feed", label: "Feed", icon: Rss },
  { to: "/explore", label: "Explore", icon: Compass },
];

// Charts (Popular, Top, Rising) live behind a single entry — the
// destination renders a tab strip so the sidebar doesn't have to.
// Popular is the default since that's the first tab on the page.
// Genres/Moods used to live here but they're already reachable from
// Explore, so keeping them in the sidebar was noise.
const discover = [
  { to: "/charts/new", label: "New Releases", icon: Newspaper },
  { to: "/popular", label: "Charts", icon: TrendingUp },
];

// Library order: the things the user curates (Liked Songs → Albums →
// Artists → Playlists) come first, then derived/supporting surfaces
// (local files, history, stats). `ListMusic` — not `Library` — for
// Playlists because the section header already uses the `Library`
// icon; two rows with the same glyph looks like a bug at a glance.
const library = [
  { to: "/library/tracks", label: "Liked Songs", icon: Heart },
  { to: "/library/albums", label: "Albums", icon: Disc3 },
  { to: "/library/artists", label: "Artists", icon: User },
  { to: "/library/playlists", label: "Playlists", icon: ListMusic },
  { to: "/library/local", label: "On this device", icon: HardDrive },
  { to: "/history", label: "History", icon: History },
  { to: "/stats", label: "Stats", icon: BarChart3 },
];

// In offline mode the only link we keep in the "Your Library" section
// is the local file list — everything else is Tidal-session-dependent.
const offlineLibrary = [
  { to: "/library/local", label: "On this device", icon: HardDrive },
];

// Shared NavLink classes. The `before:` pseudo-element is the active
// indicator — a 2px primary-colored bar pinned to the left edge of
// the active row. Inactive rows have it at h-0 (invisible); the
// active class flips it to h-5 (matching the icon height) with a
// 200 ms transition so clicking between tabs reads as the bar
// growing into the new row rather than snapping. Same idea every
// modern sidebar uses (Spotify, Linear, Notion).
const navItemClass = (isActive: boolean) =>
  cn(
    "relative flex items-center gap-4 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground",
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
  const { importLinkDismissed, set: setPrefs } = useUiPreferences();
  return (
    <aside className="flex h-full w-64 flex-col gap-2 bg-background p-2 text-sm">
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
      <NavLink
        to="/"
        end
        aria-label="Home"
        title="Home"
        className="ml-[10px] inline-flex h-10 w-10 items-center justify-center transition-transform duration-150 hover:scale-105"
      >
        <img src="/app-icon.svg" alt="" className="h-10 w-10 shrink-0" />
      </NavLink>

      {!offline && (
        <nav className="rounded-lg bg-card p-2">
          {primary.map(({ to, label, icon: Icon, end }) => {
            const badge = to === "/feed" && feedUnread > 0 ? feedUnread : 0;
            return (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) => navItemClass(isActive)}
              >
                <Icon className="h-5 w-5" />
                <span className="flex-1">{label}</span>
                {badge > 0 && (
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
          {/* Visual break between the primary tabs and the editorial
              charts — same text treatment, small uppercase label above
              to establish the group. Using a heading instead of just a
              border avoids the "three font sizes" feel the first pass
              had. */}
          <div className="mt-2 border-t border-border/50 pt-2">
            <div className="px-3 pb-1 text-[10px] font-bold uppercase tracking-wider text-muted-foreground/70">
              Discover
            </div>
            {discover.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) => navItemClass(isActive)}
              >
                <Icon className="h-5 w-5" />
                {label}
              </NavLink>
            ))}
          </div>
        </nav>
      )}

      <div className="flex min-h-0 flex-1 flex-col rounded-lg bg-card p-2">
        <div className="flex items-center justify-between px-3 py-2 text-muted-foreground">
          <span className="flex items-center gap-3 text-sm font-semibold">
            <Library className="h-5 w-5" /> Your Library
          </span>
          {!offline && (
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
          {libraryLinks.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  navItemClass(isActive),
                  // Library rows additionally fade the background on
                  // hover so this section reads as a more
                  // interaction-heavy surface than the primary /
                  // discover blocks above. Keeps the user's
                  // most-frequented area feeling distinct.
                  "hover:bg-accent",
                )
              }
            >
              <Icon className="h-5 w-5" />
              {label}
            </NavLink>
          ))}
          {!offline && !importLinkDismissed && (
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

      <div className="flex items-center gap-2">
        <NavLink
          to="/downloads"
          className={({ isActive }) =>
            cn(
              "flex flex-1 items-center gap-3 rounded-lg bg-card px-3 py-2 text-sm font-semibold text-muted-foreground hover:text-foreground",
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
          Downloads
          {activeDownloads > 0 ? (
            <span className="ml-auto rounded-full bg-primary/20 px-2 py-0.5 text-[10px] font-bold text-primary">
              {activeDownloads}
            </span>
          ) : newDownloads > 0 ? (
            <span className="ml-auto rounded-full bg-primary/20 px-2 py-0.5 text-[10px] font-bold text-primary">
              {newDownloads}
            </span>
          ) : null}
        </NavLink>
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
      </div>
    </aside>
  );
}
