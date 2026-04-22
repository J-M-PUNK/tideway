import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { LastFmPeriod, LastFmWeeklyScrobble } from "@/api/types";
import { Skeleton } from "@/components/Skeletons";
import { cn } from "@/lib/utils";

/**
 * Listening-activity bar chart: one bar per week, width governed by
 * the `period` picked on the Stats page. Bars scale to the max
 * observed count. Hovering a bar reveals an absolute date range +
 * scrobble count in the header slot above the chart.
 *
 * The short periods (7 days / 1 month) clamp at 4 weeks because a
 * single bar doesn't read as a chart. The ranked-items sections on
 * the Stats page use the exact period for their own filtering; this
 * chart is the "trend context" strip that sits above them.
 */

// Period → weeks-of-weekly-buckets. 7day rounds up to 4 so the
// chart stays visually informative; longer periods mirror their
// literal duration.
function weeksForPeriod(period: LastFmPeriod): number {
  switch (period) {
    case "7day":
      return 4;
    case "1month":
      return 4;
    case "3month":
      return 13;
    case "6month":
      return 26;
    case "12month":
      return 52;
    case "overall":
      return 52;
  }
}

export function LastFmActivityChart({
  period = "12month",
}: {
  period?: LastFmPeriod;
}) {
  const [data, setData] = useState<LastFmWeeklyScrobble[] | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const weeks = weeksForPeriod(period);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    api.lastfm
      .weeklyScrobbles(weeks)
      .then((d) => !cancelled && setData(d))
      .catch(() => !cancelled && setData([]));
    return () => {
      cancelled = true;
    };
  }, [weeks]);

  if (!data) {
    return (
      <div className="mb-8">
        <Skeleton className="mb-2 h-4 w-40" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }
  if (data.length === 0) {
    return null;
  }

  const max = Math.max(1, ...data.map((w) => w.count));
  const total = data.reduce((s, w) => s + w.count, 0);
  const hovered = hoverIdx !== null ? data[hoverIdx] : null;

  return (
    <div className="mb-10">
      <div className="mb-3 flex items-end justify-between gap-4">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Listening activity
          </div>
          <div className="mt-0.5 text-sm">
            {hovered ? (
              <>
                <span className="font-bold tabular-nums">
                  {hovered.count.toLocaleString()}
                </span>{" "}
                <span className="text-muted-foreground">
                  plays · {formatWeek(hovered)}
                </span>
              </>
            ) : (
              <>
                <span className="font-bold tabular-nums">
                  {total.toLocaleString()}
                </span>{" "}
                <span className="text-muted-foreground">
                  plays in the last {data.length} weeks
                </span>
              </>
            )}
          </div>
        </div>
      </div>
      <div
        className="flex h-32 items-end gap-0.5 rounded-lg border border-border/50 bg-card/40 p-3"
        onMouseLeave={() => setHoverIdx(null)}
      >
        {data.map((w, i) => {
          const h = Math.max(2, Math.round((w.count / max) * 100));
          const isHover = hoverIdx === i;
          return (
            <button
              key={`${w.from}`}
              onMouseEnter={() => setHoverIdx(i)}
              onFocus={() => setHoverIdx(i)}
              className={cn(
                "group flex-1 rounded-t transition-colors",
                isHover ? "bg-primary" : "bg-primary/50 hover:bg-primary/70",
              )}
              style={{ height: `${h}%` }}
              aria-label={`${w.count} plays · ${formatWeek(w)}`}
              title={`${w.count.toLocaleString()} · ${formatWeek(w)}`}
            />
          );
        })}
      </div>
    </div>
  );
}

function formatWeek(w: LastFmWeeklyScrobble): string {
  const start = new Date(w.from * 1000);
  const end = new Date(w.to * 1000 - 1000); // -1s so "week ending Sunday" reads sensibly
  const sameMonth = start.getMonth() === end.getMonth();
  const fmt = (d: Date, longMonth = true) =>
    d.toLocaleDateString(undefined, {
      month: longMonth ? "short" : undefined,
      day: "numeric",
    });
  if (sameMonth) {
    return `${fmt(start)}–${fmt(end, false)}`;
  }
  return `${fmt(start)} – ${fmt(end)}`;
}
