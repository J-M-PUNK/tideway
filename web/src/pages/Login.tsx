import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, Loader2, LogIn } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { resetQualitiesCache } from "@/hooks/useQualities";

type Mode = "pkce" | "device";

export function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [mode, setMode] = useState<Mode>("pkce");

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <div className="flex w-full max-w-md flex-col items-center gap-6 rounded-xl border border-border bg-card p-10 shadow-2xl">
        <img src="/app-icon.svg" alt="Tideway" className="h-16 w-16" />
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight">Tideway</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            High-quality music, downloaded.
          </p>
        </div>

        {mode === "pkce" ? (
          <PkceLogin onLoggedIn={onLoggedIn} onSwitchMode={() => setMode("device")} />
        ) : (
          <DeviceLogin onLoggedIn={onLoggedIn} onSwitchMode={() => setMode("pkce")} />
        )}
      </div>
    </div>
  );
}

/**
 * PKCE login — the only Tidal auth flow that unlocks Max (hi-res) downloads.
 *
 * Two paths, chosen automatically:
 *
 * 1. In-app: the packaged desktop shell opens a pywebview child window
 *    at Tidal's login URL and intercepts the `tidal://...` redirect
 *    after signin, no paste required. This is the default and lands
 *    the user in the signed-in shell as soon as the backend polls
 *    confirm logged_in.
 *
 * 2. Fallback: the dev-mode browser launch + paste the "Oops" URL.
 *    Only shown when the in-app start call returns supported=false,
 *    which happens when you run the app via ./run.sh without the
 *    packaged pywebview shell attached.
 */
function PkceLogin({
  onLoggedIn,
  onSwitchMode,
}: {
  onLoggedIn: () => void;
  onSwitchMode: () => void;
}) {
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const [redirectUrl, setRedirectUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // "inapp" is the zero-friction path the packaged app uses.
  // "paste" shows the old copy-the-URL flow when we know the shell
  // can't intercept (dev mode). Starts null until we know which
  // to render.
  const [flow, setFlow] = useState<"inapp" | "paste" | null>(null);
  const [waiting, setWaiting] = useState(false);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.auth
      .pkceUrl()
      .then((r) => {
        if (!cancelled) setLoginUrl(r.url);
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't generate a login URL.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Auth-status poll: once the in-app shell captures the redirect
  // and posts it to /api/auth/pkce/complete, the backend flips to
  // logged_in. We notice via this poll and call onLoggedIn.
  // Capped at 10 minutes so a broken shell or a user who walks
  // away doesn't leave a spinner spinning forever — matches the
  // desktop-side poll timeout.
  useEffect(() => {
    if (!waiting) return;
    const startedAt = Date.now();
    const TIMEOUT_MS = 10 * 60 * 1000;
    const tick = async () => {
      if (Date.now() - startedAt > TIMEOUT_MS) {
        setWaiting(false);
        setError("Login timed out. Please try again.");
        return;
      }
      try {
        const s = await api.auth.status();
        if (s.logged_in) {
          resetQualitiesCache();
          setWaiting(false);
          onLoggedIn();
          return;
        }
      } catch {
        // Transient — keep polling.
      }
      pollRef.current = window.setTimeout(tick, 500);
    };
    pollRef.current = window.setTimeout(tick, 500);
    return () => {
      if (pollRef.current !== null) window.clearTimeout(pollRef.current);
    };
  }, [waiting, onLoggedIn]);

  const openLoginInApp = async () => {
    if (!loginUrl) return;
    setError(null);
    try {
      const res = await api.auth.inappLoginStart();
      if (res.supported) {
        // Shell is live — it will open the login window and post the
        // redirect back for us. Start polling auth status for the
        // state change.
        setWaiting(true);
        setFlow("inapp");
        return;
      }
    } catch {
      // Shell call failed — fall through to the paste path.
    }
    // No shell: fall back to the classic open-external-browser +
    // paste flow.
    setFlow("paste");
    try {
      await api.openExternal(loginUrl);
    } catch {
      window.open(loginUrl, "_blank", "noopener");
    }
  };

  const submitPaste = async () => {
    if (!redirectUrl.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.auth.pkceComplete(redirectUrl.trim());
      resetQualitiesCache();
      onLoggedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex w-full flex-col gap-4">
      {flow !== "paste" ? (
        <>
          <p className="text-sm text-muted-foreground">
            Click below to sign in with Tidal. A small login window will
            open; once you finish, it closes automatically and you&apos;re
            in.
          </p>
          <Button
            onClick={openLoginInApp}
            disabled={!loginUrl || waiting}
            size="lg"
            className="w-full"
          >
            {waiting ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" /> Waiting for sign-in…
              </>
            ) : (
              <>
                <LogIn className="h-4 w-4" /> Sign in with Tidal
              </>
            )}
          </Button>
          {waiting && (
            <button
              onClick={() => {
                setWaiting(false);
                setFlow("paste");
              }}
              className="text-center text-xs text-muted-foreground hover:text-foreground"
            >
              Stuck? Switch to the manual paste flow.
            </button>
          )}
        </>
      ) : (
        <>
          <ol className="list-decimal space-y-2 pl-5 text-sm text-muted-foreground">
            <li>
              <button
                onClick={openLoginInApp}
                disabled={!loginUrl}
                className="text-primary hover:underline disabled:opacity-50"
              >
                Open Tidal login
              </button>{" "}
              and sign in.
            </li>
            <li>
              You&apos;ll land on a Tidal <strong>&quot;Oops&quot;</strong> page. That&apos;s expected.
            </li>
            <li>Copy the URL from that Oops page and paste it below.</li>
          </ol>

          <Button onClick={openLoginInApp} disabled={!loginUrl} size="lg" className="w-full">
            <ExternalLink className="h-4 w-4" /> Open Tidal login
          </Button>

          <div className="flex flex-col gap-2">
            <label
              htmlFor="redirect"
              className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
            >
              Paste the Oops page URL
            </label>
            <Input
              id="redirect"
              value={redirectUrl}
              onChange={(e) => setRedirectUrl(e.target.value)}
              placeholder="https://tidal.com/android/login/auth?code=…"
              onKeyDown={(e) => {
                if (e.key === "Enter") submitPaste();
              }}
            />
          </div>

          <Button
            onClick={submitPaste}
            disabled={!redirectUrl.trim() || submitting}
            size="lg"
            className="w-full"
          >
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            Continue
          </Button>
        </>
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}

      <button
        onClick={onSwitchMode}
        className="text-center text-xs text-muted-foreground hover:text-foreground"
      >
        Use the simpler code-based login instead (Lossless quality only)
      </button>
    </div>
  );
}

/**
 * Device-code login — the older, simpler "paste this code at tidal.com/link"
 * flow. Works fine but the resulting session is capped at Lossless quality
 * because tidalapi's device-code client_id isn't entitled for hi-res.
 */
function DeviceLogin({
  onLoggedIn,
  onSwitchMode,
}: {
  onLoggedIn: () => void;
  onSwitchMode: () => void;
}) {
  const [status, setStatus] = useState<"idle" | "starting" | "waiting" | "failed">("idle");
  const [info, setInfo] = useState<{ url: string; user_code: string } | null>(null);
  const pollRef = useRef<number | null>(null);

  const startLogin = useCallback(async () => {
    setStatus("starting");
    try {
      const res = await api.auth.loginStart();
      setInfo(res);
      setStatus("waiting");
      // Backend-initiated browser open; window.open is silently ignored
      // when the app runs inside pywebview. Fallback preserves plain
      // browser-mode UX.
      try {
        await api.openExternal(res.url);
      } catch {
        window.open(res.url, "_blank", "noopener");
      }
    } catch {
      setStatus("failed");
    }
  }, []);

  useEffect(() => {
    if (status !== "waiting") return;
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await api.auth.loginPoll();
        if (cancelled) return;
        if (r.status === "ok") {
          onLoggedIn();
          return;
        }
        if (r.status === "failed") {
          setStatus("failed");
          return;
        }
      } catch {
        /* keep polling */
      }
      if (cancelled) return;
      pollRef.current = window.setTimeout(tick, 1500);
    };
    pollRef.current = window.setTimeout(tick, 1500);
    return () => {
      cancelled = true;
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, [status, onLoggedIn]);

  return (
    <div className="flex w-full flex-col items-center gap-4">
      {status === "idle" && (
        <Button onClick={startLogin} size="lg" className="w-full">
          <LogIn className="h-4 w-4" /> Login with Tidal
        </Button>
      )}

      {status === "starting" && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Contacting Tidal…
        </div>
      )}

      {status === "waiting" && info && (
        <div className="flex w-full flex-col items-center gap-3">
          <div className="text-xs uppercase tracking-wider text-muted-foreground">
            Enter this code on the Tidal page
          </div>
          <div className="rounded-lg bg-secondary px-6 py-3 font-mono text-3xl font-bold tracking-widest">
            {info.user_code}
          </div>
          <a
            href={info.url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-primary hover:underline"
          >
            {info.url}
          </a>
          <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" /> Waiting for authentication…
          </div>
        </div>
      )}

      {status === "failed" && (
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-destructive">Login failed or timed out.</p>
          <Button onClick={startLogin} variant="secondary">
            Try again
          </Button>
        </div>
      )}

      <button
        onClick={onSwitchMode}
        className="text-center text-xs text-muted-foreground hover:text-foreground"
      >
        Use the hi-res login instead (unlocks Max quality)
      </button>
    </div>
  );
}
