import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { RecSectionMeta } from "@/api/client";
import type { RecSection } from "@/components/RecShelf";
import { blendTopRow } from "@/lib/recBlend";

type SectionState =
  | { status: "loading" }
  | { status: "done"; section: RecSection }
  | { status: "empty" };

export type RecItem =
  { kind: "skeleton"; key: string } | { kind: "section"; section: RecSection };

export type LazyRecs =
  | { status: "loading" }
  | { status: "disabled" }
  | { status: "error"; error: string }
  | { status: "empty" }
  | { status: "ready"; items: RecItem[] };

/**
 * Loads the For You page lazily. Fetch the cheap manifest, paint a
 * skeleton per row, then fetch every row's albums in parallel so each
 * pops in as it resolves instead of the whole page blocking on the
 * slowest build. Once every row has settled it folds them into the
 * "Recommended For You" blend exactly as the server would, so the final
 * layout matches the old one-shot page — it just gets there row by row.
 */
export function useLazyRecommendations(): LazyRecs {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [metas, setMetas] = useState<RecSectionMeta[]>([]);
  const [states, setStates] = useState<Record<string, SectionState>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .recommendationManifest()
      .then((m) => {
        if (cancelled) return;
        setEnabled(m.enabled);
        setMetas(m.sections);
        if (!m.enabled) return;
        setStates(
          Object.fromEntries(
            m.sections.map((s) => [s.key, { status: "loading" as const }]),
          ),
        );
        for (const meta of m.sections) {
          api
            .recommendationSection(meta.key)
            .then((res) => {
              if (cancelled) return;
              const s = res.section;
              setStates((prev) => ({
                ...prev,
                [meta.key]:
                  s && s.albums.length > 0
                    ? { status: "done", section: s }
                    : { status: "empty" },
              }));
            })
            .catch(() => {
              if (cancelled) return;
              setStates((prev) => ({
                ...prev,
                [meta.key]: { status: "empty" },
              }));
            });
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return { status: "error", error };
  if (enabled === null) return { status: "loading" };
  if (!enabled) return { status: "disabled" };

  const settled = metas.every((m) => states[m.key]?.status !== "loading");
  const done = metas
    .map((m) => states[m.key])
    .filter(
      (s): s is { status: "done"; section: RecSection } => s?.status === "done",
    )
    .map((s) => s.section);

  if (settled) {
    if (done.length === 0) return { status: "empty" };
    const { blend, rows } = blendTopRow(done);
    const items: RecItem[] = [];
    if (blend) items.push({ kind: "section", section: blend });
    for (const r of rows) items.push({ kind: "section", section: r });
    return { status: "ready", items };
  }

  // Some rows still loading: show what's done, skeletons for the rest,
  // and hold the blend until everything has settled so albums don't hop
  // between the blend and their source row as the page fills in.
  const items: RecItem[] = [];
  for (const meta of metas) {
    const st = states[meta.key];
    if (st?.status === "done") {
      items.push({ kind: "section", section: st.section });
    } else if (st?.status === "loading") {
      items.push({ kind: "skeleton", key: meta.key });
    }
  }
  return { status: "ready", items };
}
