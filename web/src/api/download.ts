import type { ContentKind } from "./types";

export type DownloadableKind = Extract<
  ContentKind,
  "track" | "album" | "playlist"
>;

/**
 * Shared download callback shape. Every list/card/detail passes this up,
 * every page forwards it down. Centralized so pages don't redefine it.
 */
export type OnDownload = (
  kind: DownloadableKind,
  id: string,
  quality?: string,
) => void;
