import { useNavigate } from "react-router-dom";
import { LogOut, Settings, User as UserIcon } from "lucide-react";
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
  onLogout: () => void;
}

/**
 * Top-right account menu, like Spotify / Apple Music / every streaming
 * service. Circular avatar button — shows the Tidal profile picture when
 * available, falls back to a monogram of the user's first initial. Opens a
 * dropdown with Settings, Log out, etc.
 */
export function UserMenu({ username, avatar, onLogout }: Props) {
  const navigate = useNavigate();
  const initial = (username || "?").trim().charAt(0).toUpperCase();
  const imgSrc = avatar ? imageProxy(avatar) : undefined;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="flex h-9 w-9 items-center justify-center overflow-hidden rounded-full bg-black/60 text-sm font-bold text-foreground ring-1 ring-white/10 transition-colors hover:bg-black/80 hover:ring-white/20"
          aria-label="Account menu"
          title={username ?? "Account"}
        >
          {imgSrc ? (
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
            {imgSrc ? (
              <img src={imgSrc} alt="" className="h-full w-full object-cover" />
            ) : (
              <span>{initial}</span>
            )}
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">
              {username ?? "Signed in"}
            </div>
            <div className="truncate text-[11px] font-normal text-muted-foreground">
              Tidal account
            </div>
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => navigate("/settings")}>
          <Settings className="h-4 w-4" /> Settings
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() =>
            window.open(
              `https://listen.tidal.com${
                username ? "/my-collection" : ""
              }`,
              "_blank",
              "noopener",
            )
          }
        >
          <UserIcon className="h-4 w-4" /> Open in Tidal
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={onLogout}>
          <LogOut className="h-4 w-4" /> Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
