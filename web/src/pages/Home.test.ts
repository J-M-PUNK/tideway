import { describe, expect, it } from "vitest";
import type { PageCategory, PageItem, TidalPage } from "@/api/types";
import { filterHomeRows } from "./Home";

/**
 * Home's row filter is an allowlist, so a row is excluded simply by not
 * being named — which means a typo'd or drifted title costs a whole
 * section with no error anywhere. These pin the two Tidal
 * recommendation rows that were previously hidden and are now back, and
 * pin that the allowlist still drops everything else.
 *
 * The category titles below are the real ones, captured from the live
 * `/api/page/home` feed rather than guessed, including the curly
 * apostrophes Tidal actually sends (`User playlists you’ll love`) which
 * exist to exercise `normalizeTitle`.
 */

function item(kind: string, id: string): PageItem {
  return { kind, id, name: `${kind}-${id}` } as unknown as PageItem;
}

function category(title: string, kind: string, n: number): PageCategory {
  return {
    title,
    items: Array.from({ length: n }, (_, i) => item(kind, `${title}-${i}`)),
  } as unknown as PageCategory;
}

/** The live home feed's shape: 18 categories, only a few of them ours. */
function liveHomeFeed(): TidalPage {
  return {
    categories: [
      category("Shortcuts", "album", 6),
      category("Suggested new albums for you", "album", 10),
      category("Recommended new tracks", "track", 5),
      category("Custom mixes", "mix", 10),
      category("Recently played", "album", 10),
      category("Because you listened to", "album", 9),
      category("User playlists you’ll love", "playlist", 9),
      category("Personal radio stations", "mix", 10),
      category("Uploads for you", "track", 5),
      category("Power Ballad & more", "album", 15),
      category("Albums you’ll enjoy", "album", 10),
      category("Your favorite artists", "artist", 10),
      category("Essentials to explore", "playlist", 10),
      category("Your forgotten favorites", "album", 10),
      category("Popular playlists on TIDAL", "playlist", 10),
      category("Spotlighted Uploads", "track", 5),
      category("Your listening history", "mix", 6),
    ],
  } as unknown as TidalPage;
}

const titlesOf = (cats: PageCategory[]) => cats.map((c) => c.title);

describe("filterHomeRows", () => {
  it("hoists Tidal's suggested-albums row out of the card stream", () => {
    const { hoistedAlbums, page } = filterHomeRows(liveHomeFeed());

    expect(hoistedAlbums).not.toBeNull();
    expect(hoistedAlbums?.title).toBe("Suggested new albums for you");
    expect(hoistedAlbums?.items).toHaveLength(10);
    // Hoisted means removed from the PageView stream, not duplicated
    // into it — otherwise the row renders twice.
    expect(titlesOf(page.categories)).not.toContain(
      "Suggested new albums for you",
    );
  });

  it("merges the two track rows into one compact row", () => {
    const { compactRows } = filterHomeRows(liveHomeFeed());

    const merged = compactRows.find(
      (c) => c.title === "Suggested new songs for you",
    );
    expect(merged, "merged songs row should be present").toBeDefined();
    // 5 recommended + 5 uploads, none overlapping in the fixture.
    expect(merged?.items).toHaveLength(10);
  });

  it("drops 'Recommended new tracks' from the card stream once merged", () => {
    const { page, compactRows } = filterHomeRows(liveHomeFeed());
    // The raw source titles must not survive anywhere; the merged row
    // replaces them.
    for (const bucket of [titlesOf(page.categories), titlesOf(compactRows)]) {
      expect(bucket).not.toContain("Recommended new tracks");
      expect(bucket).not.toContain("Uploads for you");
    }
  });

  it("puts Recently played before the suggested songs row", () => {
    const { compactRows } = filterHomeRows(liveHomeFeed());
    // COMPACT_ROW_TITLES declares the order, so the visual sequence is
    // independent of Tidal's feed order — in the fixture Tidal sends
    // the tracks row before Recently played.
    expect(titlesOf(compactRows)).toEqual([
      "Recently played",
      "Suggested new songs for you",
    ]);
  });

  it("still drops every row that isn't allowlisted", () => {
    const { page, compactRows, hoistedAlbums } = filterHomeRows(liveHomeFeed());
    const rendered = [
      ...titlesOf(page.categories),
      ...titlesOf(compactRows),
      ...(hoistedAlbums ? [hoistedAlbums.title] : []),
    ];
    for (const dropped of [
      "Shortcuts",
      "Because you listened to",
      "User playlists you’ll love",
      "Power Ballad & more",
      "Albums you’ll enjoy",
      "Your favorite artists",
      "Essentials to explore",
      "Your forgotten favorites",
      "Popular playlists on TIDAL",
      "Spotlighted Uploads",
      "Your listening history",
    ]) {
      expect(rendered, `${dropped} should not be rendered`).not.toContain(
        dropped,
      );
    }
  });

  it("keeps the mixes rows in their configured priority order", () => {
    const { page } = filterHomeRows(liveHomeFeed());
    expect(titlesOf(page.categories)).toEqual([
      "Custom mixes",
      "Personal radio stations",
    ]);
  });

  it("survives a feed that has none of our rows", () => {
    const page = {
      categories: [category("Shortcuts", "album", 6)],
    } as unknown as TidalPage;
    const out = filterHomeRows(page);
    expect(out.hoistedAlbums).toBeNull();
    expect(out.compactRows).toEqual([]);
    expect(out.page.categories).toEqual([]);
  });

  it("does not crash on a category with no title", () => {
    const page = {
      categories: [{ items: [] } as unknown as PageCategory],
    } as unknown as TidalPage;
    expect(() => filterHomeRows(page)).not.toThrow();
  });
});
