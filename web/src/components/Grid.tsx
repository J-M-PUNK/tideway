import type { PropsWithChildren } from "react";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";

export function Grid({ children }: PropsWithChildren) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
      {children}
    </div>
  );
}

export function SectionHeader({
  title,
  action,
}: {
  title: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="mb-4 mt-8 flex items-end justify-between">
      <h2 className="text-2xl font-bold tracking-tight">{title}</h2>
      {action}
    </div>
  );
}

/**
 * "View more →" link rendered on the right side of section headers
 * across Home, Album, Stats, Artist, and the editorial PageView. The
 * link target is what changes — wording, chevron, and styling are
 * uniform.
 */
export function ViewMoreLink({ to }: { to: string }) {
  return (
    <Link
      to={to}
      className="flex flex-shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
    >
      View more <ChevronRight className="h-3.5 w-3.5" />
    </Link>
  );
}
