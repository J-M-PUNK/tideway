import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";
import { LastfmConnectNudge } from "@/components/LastfmConnectNudge";
import type { PageCategory, PageItem, TidalPage } from "@/api/types";

// Titles of Tidal editorial rows we do not want on our home page.
// Matched case insensitively as a substring against the row title,
// normalised to strip curly Unicode apostrophes so either `'` or `'`
// in the source catches.
const HIDDEN_HOME_ROW_TITLES = [
  "albums you'll enjoy",
  "your favorite artists",
  "popular playlists",
  // Tidal renders this as a single-card row whose only item is the
  // "My Most Listened" auto-playlist. Redundant as a launcher when
  // the playlist itself is already in Your Library.
  "your listening history",
  "spotlighted uploads",
  // "Because you liked X" / "Because you listened to X" rows are a
  // long tail of duplicates of the items that sit right above them.
  // The user can still find the same recommendations inside the full
  // discovery surfaces (artist pages, mixes). Drop them from the home
  // stream so the page stays scannable.
  "because you liked",
  "because you listened",
];

// "Recommended new tracks" and "Uploads for you" both surface
// newly-added catalog content Tidal thinks we'd like. Fold them into
// a single row titled to match the sibling "Suggested new albums for
// you" row Tidal also emits, so both the songs and the albums feed
// read like a consistent section pair.
const MERGE_SOURCE_TITLES = ["recommended new tracks", "uploads for you"];
const MERGED_ROW_TITLE = "Suggested new songs for you";

// Desired order for the card-style rows at the top of the feed.
// Anything not listed here keeps whatever order Tidal sent.
const PRIORITY_ROW_ORDER = [
  "recently played",
  normalizeTitle(MERGED_ROW_TITLE),
  "suggested new albums for you",
  "custom mixes",
  "personal radio stations",
];

function normalizeTitle(s: string): string {
  return s
    .toLowerCase()
    .replace(/[‘’‛′]/g, "'") // curly / prime apostrophes
    .replace(/[“”‟″]/g, '"'); // curly / prime double quotes
}

function filterHomeRows(page: TidalPage): TidalPage {
  const hideNeedles = HIDDEN_HOME_ROW_TITLES.map(normalizeTitle);
  const mergeNeedles = MERGE_SOURCE_TITLES.map(normalizeTitle);

  // First pass: drop hidden rows, and collect the merge sources into a
  // single merged row that takes the first merge source's slot.
  const kept: PageCategory[] = [];
  const mergedItems: PageItem[] = [];
  let mergedTemplate: PageCategory | null = null;

  for (const cat of page.categories) {
    const title = normalizeTitle(cat.title ?? "");
    if (hideNeedles.some((n) => title.includes(n))) continue;
    if (mergeNeedles.some((n) => title.includes(n))) {
      if (mergedTemplate === null) {
        mergedTemplate = cat;
        kept.push(cat); // placeholder, overwritten below
      }
      for (const it of cat.items) mergedItems.push(it);
      continue;
    }
    kept.push(cat);
  }

  if (mergedTemplate) {
    const seen = new Set<string>();
    const unique = mergedItems.filter((it) => {
      const key = `${(it as { kind?: string }).kind ?? ""}:${
        (it as { id?: string }).id ?? ""
      }`;
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    const mergedIdx = kept.indexOf(mergedTemplate);
    kept[mergedIdx] = {
      ...mergedTemplate,
      title: MERGED_ROW_TITLE,
      items: unique,
    };
  }

  // Second pass: reorder the surviving rows so the priority titles
  // appear first in the configured order, with everything else
  // following in its original position.
  const priorityOrder = PRIORITY_ROW_ORDER.map(normalizeTitle);
  const priorityRows: Array<PageCategory | undefined> = priorityOrder.map(
    () => undefined,
  );
  const leftover: PageCategory[] = [];
  for (const cat of kept) {
    const title = normalizeTitle(cat.title ?? "");
    const idx = priorityOrder.indexOf(title);
    if (idx >= 0) {
      priorityRows[idx] = cat;
    } else {
      leftover.push(cat);
    }
  }
  const finalCategories: PageCategory[] = [
    ...priorityRows.filter((c): c is PageCategory => !!c),
    ...leftover,
  ];

  return { ...page, categories: finalCategories };
}

export function Home({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.page("home"), []);

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  if (loading) {
    return (
      <div>
        <h1 className="mb-6 text-4xl font-bold tracking-tight">{greeting}</h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load home"} />;

  const filteredPage = filterHomeRows(data);

  return (
    <div>
      <h1 className="mb-8 text-4xl font-bold tracking-tight">{greeting}</h1>
      <LastfmConnectNudge />
      <PageView page={filteredPage} onDownload={onDownload} forceSingleRow />
    </div>
  );
}
