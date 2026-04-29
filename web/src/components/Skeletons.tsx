import { cn } from "@/lib/utils";

/**
 * Loading-state placeholder. Uses the gradient-shimmer animation
 * defined in index.css (`animate-skeleton`) rather than Tailwind's
 * default `animate-pulse`, which fades the whole block to ~50 %
 * opacity. The shimmer keeps the surface visible while sweeping a
 * highlight across — a softer "loading" cue that doesn't strobe
 * the whole page on dense skeleton lists.
 */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-skeleton rounded-md", className)} />;
}

export function CardSkeleton() {
  return (
    <div className="flex flex-col gap-3 rounded-lg bg-card p-4">
      <Skeleton className="aspect-square w-full rounded-md" />
      <Skeleton className="h-4 w-4/5" />
      <Skeleton className="h-3 w-2/3" />
    </div>
  );
}

export function GridSkeleton({ count = 12 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
      {Array.from({ length: count }, (_, i) => (
        <CardSkeleton key={i} />
      ))}
    </div>
  );
}

export function TrackRowSkeleton() {
  return (
    <div className="grid grid-cols-[24px_4fr_3fr_48px_48px] items-center gap-4 px-4 py-2">
      <Skeleton className="h-4 w-4 rounded-full" />
      <div className="flex items-center gap-3">
        <Skeleton className="h-10 w-10 rounded" />
        <div className="flex flex-col gap-1.5">
          <Skeleton className="h-3.5 w-40" />
          <Skeleton className="h-2.5 w-24" />
        </div>
      </div>
      <Skeleton className="h-3 w-32" />
      <Skeleton className="h-3 w-8 justify-self-end" />
      <Skeleton className="h-7 w-7 justify-self-end rounded-full" />
    </div>
  );
}

export function TrackListSkeleton({ count = 8 }: { count?: number }) {
  return (
    <div className="flex flex-col">
      {Array.from({ length: count }, (_, i) => (
        <TrackRowSkeleton key={i} />
      ))}
    </div>
  );
}

export function HeroSkeleton() {
  return (
    <div className="flex flex-col items-end gap-6 md:flex-row">
      <Skeleton className="h-56 w-56 rounded-md" />
      <div className="flex flex-1 flex-col gap-3 pb-4">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-14 w-3/4" />
        <Skeleton className="h-4 w-64" />
      </div>
    </div>
  );
}
