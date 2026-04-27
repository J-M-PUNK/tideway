import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, Loader2, LogIn } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { resetQualitiesCache } from "@/hooks/useQualities";
import { refreshSubscription } from "@/hooks/useSubscription";

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
          <PkceLogin
            onLoggedIn={onLoggedIn}
            onSwitchMode={() => setMode("device")}
          />
        ) : (
          <DeviceLogin
            onLoggedIn={onLoggedIn}
            onSwitchMode={() => setMode("pkce")}
          />
        )}
      </div>
    </div>
  );
}

/**
 * PKCE login — the only Tidal auth flow that unlocks Max (hi-res)
 * downloads. Paste-the-Oops-URL flow: user clicks Open Tidal login,
 * Safari / default browser handles sign-in (every SSO provider works
 * natively there), Tidal redirects to an "Oops" page whose URL
 * carries the PKCE code as a query param, user pastes that URL, we
 * exchange the code server-side and drop into the signed-in shell.
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

  const openTidalLogin = async () => {
    if (!loginUrl) return;
    setError(null);
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
      refreshSubscription();
      onLoggedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex w-full flex-col gap-4">
      <ol className="list-decimal space-y-2 pl-5 text-sm text-muted-foreground">
        <li>
          <button
            onClick={openTidalLogin}
            disabled={!loginUrl}
            className="text-primary hover:underline disabled:opacity-50"
          >
            Open Tidal login
          </button>{" "}
          and sign in.
        </li>
        <li>
          You&apos;ll land on a Tidal <strong>&quot;Oops&quot;</strong> page.
          That&apos;s expected.
        </li>
        <li>Copy the URL from that Oops page and paste it below.</li>
      </ol>

      <Button
        onClick={openTidalLogin}
        disabled={!loginUrl}
        size="lg"
        className="w-full"
      >
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
  const [status, setStatus] = useState<
    "idle" | "starting" | "waiting" | "failed"
  >("idle");
  const [info, setInfo] = useState<{ url: string; user_code: string } | null>(
    null,
  );
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
          refreshSubscription();
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
            <Loader2 className="h-3 w-3 animate-spin" /> Waiting for
            authentication…
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
