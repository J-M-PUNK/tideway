import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

/**
 * Wire the mouse's dedicated Back (X1, button 3) and Forward (X2, button
 * 4) side buttons to router navigation (#316).
 *
 * The packaged webview doesn't map these to history the way a normal
 * browser does, so they did nothing inside Tideway even though they work
 * everywhere else. We listen on `mouseup`, which reports the X buttons
 * more reliably than `auxclick` on the webviews we ship, and call the
 * router. `navigate(1)` past the end of history is a harmless no-op.
 *
 * This covers WebView2 and WKWebView only. WebKitGTK does not deliver
 * these buttons to the DOM at all — per MDN, "On Linux (GTK), the 4th
 * button and the 5th button are not supported" — so no web-layer
 * listener can see them and Linux is handled natively in desktop.py,
 * where GDK does report them as buttons 8 and 9.
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
