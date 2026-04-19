import { NavLink } from "react-router-dom";
import {
  Compass,
  Disc3,
  Download,
  Flame,
  Heart,
  History,
  Home,
  Library,
  Link2,
  Newspaper,
  Plus,
  Rss,
  Search,
  TrendingUp,
  User,
} from "lucide-react";
import { AddUrlDialog } from "@/components/AddUrlDialog";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { cn } from "@/lib/utils";

const primary = [
  { to: "/", label: "Home", icon: Home, end: true },
  { to: "/search", label: "Search", icon: Search },
  { to: "/feed", label: "Feed", icon: Rss },
  { to: "/explore", label: "Explore", icon: Compass },
];

const discover = [
  { to: "/charts/new", label: "New Releases", icon: Newspaper },
  { to: "/charts/rising", label: "Rising", icon: Flame },
  { to: "/charts/top", label: "Top Charts", icon: TrendingUp },
];

const library = [
  { to: "/library/playlists", label: "Playlists", icon: Library },
  { to: "/library/albums", label: "Albums", icon: Disc3 },
  { to: "/library/artists", label: "Artists", icon: User },
  { to: "/library/tracks", label: "Liked Songs", icon: Heart },
  { to: "/history", label: "History", icon: History },
];

export function Sidebar({
  activeDownloads,
  newDownloads,
}: {
  activeDownloads: number;
  /** Count of completed downloads the user hasn't looked at yet. Shown
   *  only when no downloads are currently active — otherwise the active
   *  count takes precedence. */
  newDownloads: number;
}) {
  return (
    <aside className="flex h-full w-64 flex-col gap-2 bg-black p-2 text-sm">
      <nav className="rounded-lg bg-card p-2">
        {primary.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-4 rounded-md px-3 py-2 font-semibold text-muted-foreground transition-colors hover:text-foreground",
                isActive && "text-foreground",
              )
            }
          >
            <Icon className="h-5 w-5" />
            {label}
          </NavLink>
        ))}
        <div className="mt-1 border-t border-border/50 pt-1">
          {discover.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-4 rounded-md px-3 py-1.5 text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground",
                  isActive && "text-foreground",
                )
              }
            >
              <Icon className="h-4 w-4" />
              {label}
            </NavLink>
          ))}
        </div>
      </nav>

      <div className="flex min-h-0 flex-1 flex-col rounded-lg bg-card p-2">
        <div className="flex items-center justify-between px-3 py-2 text-muted-foreground">
          <span className="flex items-center gap-3 font-semibold">
            <Library className="h-5 w-5" /> Your Library
          </span>
          <CreatePlaylistDialog
            trigger={
              <button
                className="rounded-full p-1.5 hover:bg-accent hover:text-foreground"
                title="Create playlist"
              >
                <Plus className="h-4 w-4" />
              </button>
            }
          />
        </div>
        <div className="mt-1 flex flex-col gap-px overflow-y-auto scrollbar-thin">
          {library.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-4 rounded-md px-3 py-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                  isActive && "bg-accent text-foreground",
                )
              }
            >
              <Icon className="h-5 w-5" />
              {label}
            </NavLink>
          ))}
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
