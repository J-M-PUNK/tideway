import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, LogIn, Music } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";

export function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [status, setStatus] = useState<"idle" | "starting" | "waiting" | "failed">("idle");
  const [info, setInfo] = useState<{ url: string; user_code: string } | null>(null);
  const pollRef = useRef<number | null>(null);

  const startLogin = useCallback(async () => {
    setStatus("starting");
    try {
      const res = await api.auth.loginStart();
      setInfo(res);
      setStatus("waiting");
      window.open(res.url, "_blank", "noopener");
    } catch {
      setStatus("failed");
    }
  }, []);

  useEffect(() => {
    if (status !== "waiting") return;
    const tick = async () => {
      try {
        const r = await api.auth.loginPoll();
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
      pollRef.current = window.setTimeout(tick, 1500);
    };
    pollRef.current = window.setTimeout(tick, 1500);
    return () => {
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, [status, onLoggedIn]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <div className="flex w-full max-w-md flex-col items-center gap-6 rounded-xl border border-border bg-card p-10 shadow-2xl">
        <div className="rounded-full bg-primary/10 p-4 text-primary">
          <Music className="h-8 w-8" />
        </div>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight">Tidal Downloader</h1>
          <p className="mt-1 text-sm text-muted-foreground">High-quality music, downloaded.</p>
        </div>

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
      </div>
    </div>
  );
}
