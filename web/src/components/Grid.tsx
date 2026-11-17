import type { PropsWithChildren } from "react";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { cardGridTemplate } from "@/hooks/useCardColumns";

/**
 * Card grid. Cards keep to a fixed size range and a wider space fits
 * more of them, rather than the same number stretched wider.
 *
 * Pass `columns` from `useCardColumns` in a single-row section: the
 * section slices its items to that count and the grid lays out exactly
 * that many columns, so nothing can wrap to a second row. Without it,
 * the browser fits as many columns as the width allows.
 */
export function Grid({
  children,
  columns,
}: PropsWithChildren<{ columns?: number }>) {
  return (
    <div
      className="grid gap-4"
      style={{ gridTemplateColumns: cardGridTemplate(columns) }}
    >
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
      className="flex shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
    >
      View more <ChevronRight className="h-3.5 w-3.5" />
    </Link>
  );
}
