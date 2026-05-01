import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";

/**
 * Custom-drawn window chrome for the desktop shell.
 *
 * On Windows the launcher creates the pywebview window with
 * `frameless=True`, so the OS no longer paints a caption row with
 * minimize / maximize / close. This component draws those buttons in
 * HTML and routes their clicks through `/api/_internal/window/*` back
 * to the pywebview window.
 *
 * On macOS we use FullSizeContentView in app/window_chrome.py so
 * WKWebView paints the page body under the OS titlebar — the
 * traffic-light area gets the same near-black as the rest of the
 * app, no gray system band. We render a 28px React spacer here so
 * page content (the NavBar, scroll regions, etc.) doesn't sit
 * directly under the traffic lights.
 *
 * Native window drag and double-click-to-zoom in that 28px band
 * are restored via a transparent NSView overlay (also installed by
 * window_chrome.py) whose `mouseDownCanMoveWindow` returns YES.
 * AppKit's hit-test finds the overlay first, sees the YES, and
 * routes the click to the native drag/zoom handler before it ever
 * reaches WKWebView's JS layer. The React spacer is just visual
 * spacing — it never sees the mouse events.
 *
 * Plain browser dev mode and Linux render nothing.
 *
 * Drag region (Windows only): WebView2 silently ignores CSS
 * `app-region: drag`, so on mousedown we hand off to a backend
 * endpoint that runs Win32's move loop directly.
 */

type Platform = "win32" | "darwin" | "linux" | "browser";

interface ChromeInfo {
  platform: Platform;
  frameless: boolean;
  maximized: boolean;
}

const DEFAULT_INFO: ChromeInfo = {
  platform: "browser",
  frameless: false,
  maximized: false,
};

/** Infer platform without a backend round-trip so first paint can
 *  reserve space immediately on macOS (avoiding a flash of content
 *  under the traffic lights). The /info endpoint refines this once
 *  it resolves. */
function inferPlatformFromUA(): Platform {
  if (typeof navigator === "undefined") return "browser";
  const ua = navigator.userAgent.toLowerCase();
  if (ua.includes("windows")) return "win32";
  if (ua.includes("mac os") || ua.includes("macintosh")) return "darwin";
  if (ua.includes("linux")) return "linux";
  return "browser";
}

export function WindowTitlebar() {
  const [info, setInfo] = useState<ChromeInfo>(() => ({
    ...DEFAULT_INFO,
    platform: inferPlatformFromUA(),
  }));
  // Last-mousedown tracker for manual double-click detection — see
  // `onTitlebarMouseDown` below for why we can't use React's
  // built-in onDoubleClick. Lives at the top of the component so
  // it's called unconditionally on every render, regardless of
  // which platform branch returns early further down.
  const lastMouseDownRef = useRef<{ t: number; x: number; y: number }>({
    t: 0,
    x: 0,
    y: 0,
  });

  useEffect(() => {
    let cancelled = false;
    api.window
      .info()
      .then((res) => {
        if (cancelled) return;
        if (!res || !("ok" in res) || !res.ok) return;
        setInfo({
          platform: (res.platform as Platform) ?? "browser",
          frameless: !!res.frameless,
          maximized: !!res.maximized,
        });
      })
      .catch(() => {
        /* dev mode without launcher — leave UA-based default */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Track maximized state on Windows via window-resize heuristics.
  // This catches state changes the user makes outside our buttons:
  // Win+Up, drag-to-edge snap, double-click on the drag region.
  // outerWidth/outerHeight match availWidth/availHeight when
  // maximized; we accept a 2px slack for DPI-rounding edge cases.
  useEffect(() => {
    if (info.platform !== "win32" || !info.frameless) return;
    const check = () => {
      const w = window.outerWidth;
      const h = window.outerHeight;
      const sw = window.screen.availWidth;
      const sh = window.screen.availHeight;
      const isMax = Math.abs(w - sw) <= 2 && Math.abs(h - sh) <= 2;
      setInfo((prev) =>
        prev.maximized === isMax ? prev : { ...prev, maximized: isMax },
      );
    };
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, [info.platform, info.frameless]);

  // Hide entirely on Linux (no integrated chrome) and in plain
  // browser dev mode (browser provides everything).
  if (info.platform === "linux" || info.platform === "browser") {
    return null;
  }

  // macOS: 28px spacer reserving the traffic-light zone. The
  // FullSizeContentView style mask in app/window_chrome.py extends
  // WKWebView under the OS titlebar so the spacer's bg-background
  // shows behind the traffic lights — visual blend with the rest
  // of the app. Mouse events here are caught by the transparent
  // NSView overlay installed by window_chrome.py before they
  // reach this React component, so the spacer doesn't need (and
  // shouldn't have) any mouse handlers — drag and double-click
  // happen at the native AppKit layer.
  if (info.platform === "darwin") {
    return (
      <div
        className="select-none bg-background"
        style={{ height: 28 }}
        aria-hidden="true"
      />
    );
  }

  // The mini-player window is created with frameless=False (it's a
  // tiny floating panel where the native chrome is the right call —
  // users expect to be able to drag and close it through the OS).
  // The /info endpoint reports the *main* window's frameless flag,
  // so we have to detect the mini route ourselves and skip our
  // controls when this React tree is hosted in the mini window.
  const isMiniRoute =
    typeof window !== "undefined" &&
    window.location.pathname.startsWith("/mini");

  // Windows: full custom titlebar with min / max / close on the right
  // and a draggable region taking the rest. Only render the controls
  // when the window is actually frameless and this isn't the mini
  // player — otherwise the OS still owns the caption row and a
  // second set of buttons would be confusing.
  if (info.platform === "win32" && (!info.frameless || isMiniRoute)) {
    return null;
  }

  // Drag handler. WebView2 silently ignores `-webkit-app-region: drag`
  // (it's a Chromium-Apps feature, not stock Chromium), so on
  // mousedown we hand off to a backend endpoint that runs Win32's
  // move loop directly. Once the OS enters that loop it swallows
  // the matching mouseup before the browser sees it, which means
  // React's onDoubleClick never fires — the second mousedown looks
  // like a brand-new first click. Detect double-click manually by
  // tracking the last mousedown timestamp + position; on the second
  // mousedown within ~500 ms and within a few pixels, route to
  // maximize instead of drag. Same threshold the OS uses for its
  // own caption double-click.
  const DOUBLE_CLICK_MS = 500;
  const DOUBLE_CLICK_PX = 4;
  const onTitlebarMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest("button")) return;
    e.preventDefault();
    const now = performance.now();
    const last = lastMouseDownRef.current;
    const isDoubleClick =
      now - last.t < DOUBLE_CLICK_MS &&
      Math.abs(e.clientX - last.x) <= DOUBLE_CLICK_PX &&
      Math.abs(e.clientY - last.y) <= DOUBLE_CLICK_PX;
    lastMouseDownRef.current = { t: now, x: e.clientX, y: e.clientY };
    if (isDoubleClick) {
      // Reset so a third quick click doesn't double-toggle.
      lastMouseDownRef.current = { t: 0, x: 0, y: 0 };
      api.window
        .maximize()
        .then((res) => {
          if (typeof res.maximized === "boolean") {
            setInfo((prev) => ({ ...prev, maximized: res.maximized! }));
          }
        })
        .catch(() => {});
      return;
    }
    api.window.startDrag().catch(() => {});
  };

  return (
    <div
      className="flex select-none items-center bg-background text-foreground"
      style={{ height: 32 }}
      onMouseDown={onTitlebarMouseDown}
    >
      <div className="flex items-center gap-2 pl-3 text-xs font-medium opacity-70">
        Tideway
      </div>
      <div className="flex-1" />
      <div className="flex h-full">
        <TitlebarButton
          ariaLabel="Minimize"
          onClick={() => {
            api.window.minimize().catch(() => {});
          }}
        >
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
            <line
              x1="0"
              y1="5.5"
              x2="10"
              y2="5.5"
              stroke="currentColor"
              strokeWidth="1"
            />
          </svg>
        </TitlebarButton>
        <TitlebarButton
          ariaLabel={info.maximized ? "Restore" : "Maximize"}
          onClick={() => {
            api.window
              .maximize()
              .then((res) => {
                if (typeof res.maximized === "boolean") {
                  setInfo((prev) => ({ ...prev, maximized: res.maximized! }));
                }
              })
              .catch(() => {});
          }}
        >
          {info.maximized ? <RestoreIcon /> : <MaximizeIcon />}
        </TitlebarButton>
        <TitlebarButton
          ariaLabel="Close"
          danger
          onClick={() => {
            api.window.close().catch(() => {});
          }}
        >
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
            <line
              x1="0.5"
              y1="0.5"
              x2="9.5"
              y2="9.5"
              stroke="currentColor"
              strokeWidth="1"
            />
            <line
              x1="9.5"
              y1="0.5"
              x2="0.5"
              y2="9.5"
              stroke="currentColor"
              strokeWidth="1"
            />
          </svg>
        </TitlebarButton>
      </div>
    </div>
  );
}

function TitlebarButton({
  children,
  onClick,
  ariaLabel,
  danger = false,
}: {
  children: React.ReactNode;
  onClick: () => void;
  ariaLabel: string;
  // Close gets the standard red hover state; min/max get a neutral
  // tint matching Windows' default chrome.
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      className={
        "flex h-full items-center justify-center text-foreground/70 transition-colors hover:text-foreground " +
        (danger
          ? "hover:bg-red-600 hover:text-white"
          : "hover:bg-foreground/10")
      }
      style={{ width: 46 }}
    >
      {children}
    </button>
  );
}

function MaximizeIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      <rect
        x="0.5"
        y="0.5"
        width="9"
        height="9"
        fill="none"
        stroke="currentColor"
        strokeWidth="1"
      />
    </svg>
  );
}

function RestoreIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      <rect
        x="2.5"
        y="0.5"
        width="7"
        height="7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1"
      />
      <rect
        x="0.5"
        y="2.5"
        width="7"
        height="7"
        fill="var(--background, currentColor)"
        stroke="currentColor"
        strokeWidth="1"
      />
    </svg>
  );
}
