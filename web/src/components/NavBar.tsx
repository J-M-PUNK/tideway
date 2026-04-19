import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Back/forward buttons that mirror react-router's location stack.
 *
 * react-router-dom writes `idx` into `history.state` on every navigation,
 * which gives us a reliable position counter even across page reloads. We
 * track a `maxDepth` alongside the current `idx` so forward is only
 * available when there's somewhere forward to go.
 */
function readIdx(): number {
  const state = window.history.state as { idx?: number } | null;
  return state?.idx ?? 0;
}

export function NavBar() {
  const navigate = useNavigate();
  const location = useLocation();
  // Seed from the browser history on mount so a mid-session reload doesn't
  // zero the counter.
  const [depth, setDepth] = useState<number>(() => readIdx());
  const [maxDepth, setMaxDepth] = useState<number>(() => readIdx());
  const lastKeyRef = useRef(location.key);

  useEffect(() => {
    if (location.key === lastKeyRef.current) return;
    lastKeyRef.current = location.key;
    const idx = readIdx();
    setDepth(idx);
    setMaxDepth((m) => Math.max(m, idx));
  }, [location.key]);

  const canBack = depth > 0;
  const canForward = depth < maxDepth;

  return (
    <div className="sticky top-0 z-10 -mx-8 -mt-6 flex items-center gap-2 bg-background/50 px-8 py-3 backdrop-blur-sm">
      <button
        onClick={() => canBack && navigate(-1)}
        disabled={!canBack}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-full bg-black/40 text-foreground hover:bg-black/60 disabled:cursor-not-allowed disabled:opacity-40",
        )}
        aria-label="Go back"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <button
        onClick={() => canForward && navigate(1)}
        disabled={!canForward}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-full bg-black/40 text-foreground hover:bg-black/60 disabled:cursor-not-allowed disabled:opacity-40",
        )}
        aria-label="Go forward"
      >
        <ChevronRight className="h-4 w-4" />
      </button>
    </div>
  );
}
