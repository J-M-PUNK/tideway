import { NavLink } from "react-router-dom";
import { Flame, Globe, TrendingUp } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Shared tab header for the Charts family of pages. Tidal Top + Rising
 * (editorial) and Last.fm Popular (crowd-sourced) all sit behind one
 * sidebar entry; this strip is how the user switches between them.
 */
const TABS = [
  { to: "/popular", label: "Popular", icon: Globe },
  { to: "/charts/top", label: "Top", icon: TrendingUp },
  { to: "/charts/rising", label: "Rising", icon: Flame },
];

export function ChartsNav() {
  return (
    <div className="mb-8 flex items-center gap-2 border-b border-border/50">
      {TABS.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-semibold transition-colors",
              isActive
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )
          }
        >
          <Icon className="h-4 w-4" />
          {label}
        </NavLink>
      ))}
    </div>
  );
}
