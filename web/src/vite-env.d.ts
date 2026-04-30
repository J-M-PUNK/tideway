/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SHOW_AIRPLAY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// app-region (and the -webkit-app-region alias) is a Chromium /
// WebView2-specific CSS property used to declare drag regions inside
// frameless windows. The DOM lib doesn't ship typings for it, so we
// augment React's CSSProperties to allow it on inline `style` props.
// See: https://developer.mozilla.org/en-US/docs/Web/CSS/-webkit-app-region
import "react";
declare module "react" {
  interface CSSProperties {
    WebkitAppRegion?: "drag" | "no-drag";
    appRegion?: "drag" | "no-drag";
  }
}
