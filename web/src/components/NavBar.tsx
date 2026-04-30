import { useEffect, useRef, useState } from "react";
import {
  useLocation,
  useNavigate,
  useNavigationType,
  useSearchParams,
} from "react-router-dom";
import { ChevronLeft, ChevronRight, Search as SearchIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { SearchSuggestions } from "@/components/SearchSuggestions";
import { UserMenu } from "@/components/UserMenu";
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

interface NavBarProps {
  username: string | null;
  avatar: string | null;
  userId?: string | null;
  onLogout: () => void;
  offline?: boolean;
  onSignInRequested?: () => void;
}

export function NavBar({
  username,
  avatar,
  userId = null,
  onLogout,
  offline = false,
  onSignInRequested,
}: NavBarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const navigationType = useNavigationType();
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
    // A PUSH after Back truncates the browser's forward stack — the old
    // forward chain is gone. Reset maxDepth to the new idx so the forward
    // button doesn't stay enabled pointing at entries that no longer
    // exist. REPLACE keeps the stack intact (history.replaceState doesn't
    // touch forward entries), so treat it like POP and preserve maxDepth.
    if (navigationType === "PUSH") {
      setMaxDepth(idx);
    } else {
      setMaxDepth((m) => Math.max(m, idx));
    }
  }, [location.key, navigationType]);

  const canBack = depth > 0;
  const canForward = depth < maxDepth;

  return (
    <div className="sticky top-0 z-10 flex items-center gap-3 bg-background/50 px-8 py-3 backdrop-blur-sm">
      <button
        onClick={() => canBack && navigate(-1)}
        disabled={!canBack}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-full bg-foreground/10 text-foreground hover:bg-foreground/20 disabled:cursor-not-allowed disabled:opacity-40",
        )}
        aria-label="Go back"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <button
        onClick={() => canForward && navigate(1)}
        disabled={!canForward}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-full bg-foreground/10 text-foreground hover:bg-foreground/20 disabled:cursor-not-allowed disabled:opacity-40",
        )}
        aria-label="Go forward"
      >
        <ChevronRight className="h-4 w-4" />
      </button>
      <NavBarSearch className="ml-auto" />
      <UserMenu
        username={username}
        avatar={avatar}
        userId={userId}
        onLogout={onLogout}
        offline={offline}
        onSignInRequested={onSignInRequested}
      />
    </div>
  );
}

/**
 * Always-visible search input in the top bar. Typing here navigates to
 * /search?q=<query>; the Search page reads its query back out of the
 * URL so both stays in sync. Empty input on a non-Search route does
 * nothing — we only push once there's something to look for.
 *
 * While the input has focus and contains text, an inline typeahead
 * dropdown ({@link SearchSuggestions}) hangs underneath it with a
 * compact preview of the top matches across all kinds. Picking a row
 * navigates to that result; pressing Enter without a selection just
 * stays on the live-updating Search page.
 */
function NavBarSearch({ className }: { className?: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();
  // Seed from the URL so navigating directly to /search?q=foo shows
  // "foo" in the input.
  const [value, setValue] = useState(() => params.get("q") ?? "");
  const [focused, setFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Keep the input in sync when the URL changes from elsewhere (e.g.
  // clicking a saved search, using Back / Forward, external links).
  useEffect(() => {
    const q = params.get("q") ?? "";
    // Only overwrite if different, so the effect doesn't fight the
    // user's live keystrokes.
    setValue((prev) => (prev === q ? prev : q));
  }, [params]);

  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const next = e.target.value;
    setValue(next);
    // Update the URL in place. Replace when we're already on /search so
    // a typing session doesn't flood history; push when entering from
    // another page so Back returns where you were.
    const target = `/search${next ? `?q=${encodeURIComponent(next)}` : ""}`;
    if (location.pathname === "/search") {
      navigate(target, { replace: true });
    } else {
      navigate(target);
    }
  };

  // Dropdown is "open" only when both: the input has focus AND there's
  // something to search for. Empty + focused renders nothing — no
  // gratuitous "Start typing" panel, that's the placeholder's job.
  const dropdownOpen = focused && value.trim().length > 0;

  const closeDropdown = () => {
    setFocused(false);
    inputRef.current?.blur();
  };

  return (
    <div className={cn("relative w-72", className)}>
      <SearchIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        ref={inputRef}
        value={value}
        onChange={onChange}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        placeholder="Search"
        aria-label="Search"
        // Search queries are artist / track / album names; the OS's
        // autocorrect, autocomplete, autocapitalize, and spellcheck
        // helpers don't know any of them and consistently rewrite
        // legitimate names into nonsense ("Diplo" -> "Diploma",
        // "Phoebe Bridgers" -> "Phoebe Burgers", etc.). Disabling all
        // four matches what Spotify, Apple Music, and YouTube Music do
        // in their own search bars. `enterKeyHint="search"` is a small
        // mobile nicety: the on-screen keyboard's enter key labels as
        // a magnifying glass.
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        enterKeyHint="search"
        className="h-9 pl-9"
      />
      <SearchSuggestions
        query={value}
        open={dropdownOpen}
        onActivate={closeDropdown}
        onCloseRequested={closeDropdown}
      />
    </div>
  );
}
