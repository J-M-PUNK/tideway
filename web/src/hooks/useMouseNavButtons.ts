import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

/**
 * Wire the mouse's dedicated Back (X1 / button 3) and Forward (X2 /
 * button 4) side buttons to router navigation (#316).
 *
 * The packaged webview doesn't map these to history the way a normal
 * browser does, so they did nothing inside Tideway even though they work
 * everywhere else. We listen on `mouseup` — the event that reliably
 * reports the X buttons across the webviews we ship (WebView2 / WebKit),
 * where `auxclick` coverage for them is patchier — and call the router.
 * `navigate(1)` past the end of history is a harmless no-op.
 */
export function useMouseNavButtons(): void {
  const navigate = useNavigate();
  useEffect(() => {
    const onMouseUp = (e: MouseEvent) => {
      if (e.button === 3) {
        e.preventDefault();
        navigate(-1);
      } else if (e.button === 4) {
        e.preventDefault();
        navigate(1);
      }
    };
    window.addEventListener("mouseup", onMouseUp);
    return () => window.removeEventListener("mouseup", onMouseUp);
  }, [navigate]);
}
