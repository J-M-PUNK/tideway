import { describe, expect, it } from "vitest";
import type { Album } from "@/api/types";
import { sortAlbums } from "./ArtistSection";

function album(partial: Partial<Album>): Album {
  return {
    kind: "album",
    id: partial.id ?? "0",
    name: partial.name ?? "untitled",
    num_tracks: partial.num_tracks ?? 1,
    duration: partial.duration ?? 0,
    year: partial.year ?? null,
    cover: null,
    artists: [],
    explicit: false,
    release_date: partial.release_date ?? null,
    ...partial,
  };
}

const fixtures: Album[] = [
  album({ id: "a", name: "Bee", release_date: "2020-03-15", year: 2020 }),
  album({ id: "b", name: "Apple", release_date: "2018-07-01", year: 2018 }),
  album({ id: "c", name: "Cherry", release_date: "2024-11-22", year: 2024 }),
];

describe("sortAlbums", () => {
  it("orders newest first by default semantics (release_date desc)", () => {
    const out = sortAlbums(fixtures, "newest");
    expect(out.map((a) => a.id)).toEqual(["c", "a", "b"]);
  });

  it("orders oldest first when asked", () => {
    const out = sortAlbums(fixtures, "oldest");
    expect(out.map((a) => a.id)).toEqual(["b", "a", "c"]);
  });

  it("orders alphabetically when asked", () => {
    const out = sortAlbums(fixtures, "alpha");
    expect(out.map((a) => a.id)).toEqual(["b", "a", "c"]);
  });

  it("does not mutate the input array", () => {
    const original = [...fixtures];
    sortAlbums(fixtures, "newest");
    expect(fixtures.map((a) => a.id)).toEqual(original.map((a) => a.id));
  });

  it("falls back to the `year` field when release_date is missing", () => {
    const items = [
      album({ id: "x", name: "X", release_date: null, year: 1999 }),
      album({ id: "y", name: "Y", release_date: null, year: 2010 }),
    ];
    expect(sortAlbums(items, "newest").map((a) => a.id)).toEqual(["y", "x"]);
    expect(sortAlbums(items, "oldest").map((a) => a.id)).toEqual(["x", "y"]);
  });

  it("buries albums with no date at the back of newest-first", () => {
    const items = [
      album({ id: "n", name: "N", release_date: null, year: null }),
      album({ id: "d", name: "D", release_date: "2020-01-01", year: 2020 }),
    ];
    expect(sortAlbums(items, "newest").map((a) => a.id)).toEqual(["d", "n"]);
  });

  it("alphabetical sort is case-insensitive", () => {
    const items = [
      album({ id: "1", name: "banana" }),
      album({ id: "2", name: "Apple" }),
      album({ id: "3", name: "cherry" }),
    ];
    expect(sortAlbums(items, "alpha").map((a) => a.id)).toEqual([
      "2",
      "1",
      "3",
    ]);
  });

  it("preserves input order on ties (stable sort)", () => {
    const items = [
      album({ id: "first", name: "Same", release_date: "2020-01-01" }),
      album({ id: "second", name: "Same", release_date: "2020-01-01" }),
      album({ id: "third", name: "Same", release_date: "2020-01-01" }),
    ];
    expect(sortAlbums(items, "alpha").map((a) => a.id)).toEqual([
      "first",
      "second",
      "third",
    ]);
    expect(sortAlbums(items, "newest").map((a) => a.id)).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  it("handles empty input", () => {
    expect(sortAlbums([], "newest")).toEqual([]);
    expect(sortAlbums([], "oldest")).toEqual([]);
    expect(sortAlbums([], "alpha")).toEqual([]);
  });
});
