/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SHOW_AIRPLAY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
