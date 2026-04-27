import { useNavigate } from "react-router-dom";
import {
  LogIn,
  LogOut,
  PictureInPicture2,
  Power,
  Settings,
  User as UserIcon,
  WifiOff,
} from "lucide-react";
import { api } from "@/api/client";
import { useOfflineMode } from "@/hooks/useOfflineMode";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { imageProxy } from "@/lib/utils";

interface Props {
  username: string | null;
  avatar: string | null;
  userId?: string | null;
  onLogout: () => void;
  /** App-level offline toggle. Hides online-only menu entries and
   *  shows an offline indicator. Independent of sign-in state: a
   *  signed-in user in offline mode still sees Log out, not Sign in. */
  offline?: boolean;
  onSignInRequested?: () => void;
}

/**
 * Top-right account menu, like Spotify / Apple Music / every streaming
 * service. Circular avatar button — shows the Tidal profile picture when
 * available, falls back to a monogram of the user's first initial. Opens a
 * dropdown with Settings, Log out, etc.
 */
export function UserMenu({
  username,
  avatar,
  userId = null,
  onLogout,
  offline = false,
  onSignInRequested,
}: Props) {
  const navigate = useNavigate();
  const { set: setOffline } = useOfflineMode();
  const initial = (username || "?").trim().charAt(0).toUpperCase();
  const imgSrc = avatar ? imageProxy(avatar) : undefined;
  const signedIn = username !== null;

  const handleSignIn = () => {
    // Flip the LOCAL offline context so App falls through to the Login
    // screen. We deliberately don't persist offline_mode=false to the
    // server — if the user abandons sign-in at the Login page, a reload
    // restores their preference and drops them back into offline mode.
    // Login is the only way forward; there's no offline toggle there.
    setOffline(false);
    onSignInRequested?.();
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="flex h-9 w-9 items-center justify-center overflow-hidden rounded-full bg-secondary text-sm font-bold text-foreground ring-1 ring-border transition-colors hover:bg-accent hover:ring-border"
          aria-label="Account menu"
          title={
            offline
              ? signedIn
                ? `${username} (offline)`
                : "Offline"
              : (username ?? "Account")
          }
        >
          {offline ? (
            <WifiOff className="h-4 w-4 text-muted-foreground" />
          ) : imgSrc ? (
            <img
              src={imgSrc}
              alt=""
              className="h-full w-full object-cover"
              onError={(e) => {
                // If the image fails to load (e.g. 404 from Tidal's CDN),
                // hide it so the fallback initial shows through.
                (e.currentTarget as HTMLImageElement).style.display = "none";
              }}
            />
          ) : (
            <span className="select-none">{initial}</span>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="flex items-center gap-3">
          <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center overflow-hidden rounded-full bg-secondary text-xs font-bold">
            {offline ? (
              <WifiOff className="h-4 w-4 text-muted-foreground" />
            ) : imgSrc ? (
              <img src={imgSrc} alt="" className="h-full w-full object-cover" />
            ) : (
              <span>{initial}</span>
            )}
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">
              {signedIn ? username : "Offline"}
            </div>
            <div className="truncate text-[11px] font-normal text-muted-foreground">
              {offline
                ? signedIn
                  ? "Offline mode"
                  : "Local files only"
                : "Tidal account"}
            </div>
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {signedIn && userId && !offline && (
          <DropdownMenuItem onSelect={() => navigate(`/user/${userId}`)}>
            <UserIcon className="h-4 w-4" /> My profile
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onSelect={() => navigate("/settings")}>
          <Settings className="h-4 w-4" /> Settings
        </DropdownMenuItem>
        {!offline && (
          <DropdownMenuItem
            onSelect={async () => {
              const url = `https://listen.tidal.com${
                username ? "/my-collection" : ""
              }`;
              try {
                // Go through the backend — pywebview's embedded WebView
                // doesn't honor `window.open` for external URLs on any
                // platform. Fallback to window.open covers plain browser
                // dev mode.
                await api.openExternal(url);
              } catch {
                window.open(url, "_blank", "noopener");
              }
            }}
          >
            <UserIcon className="h-4 w-4" /> Open in Tidal
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          onSelect={async () => {
            // No-ops silently in plain-browser dev mode (no launcher
            // to create the second window). In the packaged app the
            // response is {ok: true} and pywebview spawns the window.
            try {
              await api.openMiniPlayer();
            } catch {
              /* ignore */
            }
          }}
        >
          <PictureInPicture2 className="h-4 w-4" /> Open mini-player
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {signedIn ? (
          <DropdownMenuItem onSelect={onLogout}>
            <LogOut className="h-4 w-4" /> Log out
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem onSelect={handleSignIn}>
            <LogIn className="h-4 w-4" /> Sign in
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          onSelect={async () => {
            // Best-effort: the endpoint only works when running inside
            // the pywebview launcher (which registers the quit
            // callback). In plain-browser dev mode it returns
            // {ok:false,reason:"no launcher"} and we silently no-op.
            try {
              await api.quitApp();
            } catch {
              /* ignore — fetch errors are expected mid-shutdown */
            }
          }}
        >
          <Power className="h-4 w-4" /> Quit Tideway
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
