import { useEffect, useState } from "react";
import { api } from "@/api/client";

/**
 * Eight invisible hit strips around the window edge that translate a
 * mousedown into a native resize. Required because the WebView2 child
 * window covers the entire client area, so WS_THICKFRAME's resize
 * zones never reach the user — we paint our own zones in HTML, then
 * route the click through to Win32's SC_SIZE handler on the backend.
 *
 * Only renders on Windows + frameless. Plain browser, macOS, and
 * Linux all leave resize to the OS.
 *
 * Edge thickness is 6 CSS px; corner zones are 12 CSS px square so
 * cursor diagonal-resize hover is forgiving. Z-index sits above the
 * page body but below modals (`fixed` + `z-50` matches the existing
 * sidebar/modal stack — we never want the strips eating clicks on
 * a dialog backdrop).
 */

type Direction =
  | "left"
  | "right"
  | "top"
  | "bottom"
  | "topleft"
  | "topright"
  | "bottomleft"
  | "bottomright";

interface ChromeInfo {
  platform: string;
  frameless: boolean;
}

const EDGE_PX = 6;
const CORNER_PX = 12;

export function WindowResizeEdges() {
  const [info, setInfo] = useState<ChromeInfo>({
    platform: "browser",
    frameless: false,
  });

  useEffect(() => {
    let cancelled = false;
    api.window
      .info()
      .then((res) => {
        if (cancelled) return;
        if (!res || !("ok" in res) || !res.ok) return;
        setInfo({
          platform: res.platform ?? "browser",
          frameless: !!res.frameless,
        });
      })
      .catch(() => {
        /* dev mode / no launcher — leave defaults */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (info.platform !== "win32" || !info.frameless) return null;

  const start = (dir: Direction) => (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    api.window.startResize(dir).catch(() => {});
  };

  // Common style fragment for an edge strip: pointer events on, no
  // background paint, `select-none` so click+drag can't accidentally
  // start a text selection. Cursor hints come from each direction.
  const baseEdge: React.CSSProperties = {
    position: "fixed",
    pointerEvents: "auto",
    zIndex: 50,
  };

  return (
    <>
      {/* Edges */}
      <div
        onMouseDown={start("top")}
        style={{
          ...baseEdge,
          top: 0,
          left: CORNER_PX,
          right: CORNER_PX,
          height: EDGE_PX,
          cursor: "ns-resize",
        }}
      />
      <div
        onMouseDown={start("bottom")}
        style={{
          ...baseEdge,
          bottom: 0,
          left: CORNER_PX,
          right: CORNER_PX,
          height: EDGE_PX,
          cursor: "ns-resize",
        }}
      />
      <div
        onMouseDown={start("left")}
        style={{
          ...baseEdge,
          left: 0,
          top: CORNER_PX,
          bottom: CORNER_PX,
          width: EDGE_PX,
          cursor: "ew-resize",
        }}
      />
      <div
        onMouseDown={start("right")}
        style={{
          ...baseEdge,
          right: 0,
          top: CORNER_PX,
          bottom: CORNER_PX,
          width: EDGE_PX,
          cursor: "ew-resize",
        }}
      />
      {/* Corners — slightly larger square so diagonal hover is easier
          to grab than the 6px edge strip alone allows. */}
      <div
        onMouseDown={start("topleft")}
        style={{
          ...baseEdge,
          top: 0,
          left: 0,
          width: CORNER_PX,
          height: CORNER_PX,
          cursor: "nwse-resize",
        }}
      />
      <div
        onMouseDown={start("topright")}
        style={{
          ...baseEdge,
          top: 0,
          right: 0,
          width: CORNER_PX,
          height: CORNER_PX,
          cursor: "nesw-resize",
        }}
      />
      <div
        onMouseDown={start("bottomleft")}
        style={{
          ...baseEdge,
          bottom: 0,
          left: 0,
          width: CORNER_PX,
          height: CORNER_PX,
          cursor: "nesw-resize",
        }}
      />
      <div
        onMouseDown={start("bottomright")}
        style={{
          ...baseEdge,
          bottom: 0,
          right: 0,
          width: CORNER_PX,
          height: CORNER_PX,
          cursor: "nwse-resize",
        }}
      />
    </>
  );
}
