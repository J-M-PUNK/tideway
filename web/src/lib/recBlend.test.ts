import { describe, it, expect } from "vitest";
import { blendTopRow } from "./recBlend";
import type { RecAlbum, RecSection } from "@/components/RecShelf";

function album(id: string): RecAlbum {
  return { id, name: id, artists: [] } as unknown as RecAlbum;
}
function section(key: string, ids: string[]): RecSection {
  return { key, title: key, subtitle: "", albums: ids.map(album) };
}

describe("blendTopRow", () => {
  it("returns no blend for fewer than two non-empty rows", () => {
    const rows = [section("a", ["a1", "a2", "a3", "a4"])];
    const out = blendTopRow(rows);
    expect(out.blend).toBeNull();
    expect(out.rows).toBe(rows);
  });

  it("declines to blend fewer than four picks", () => {
    // Two rows, one album each → only two picks, below the server's floor.
    const out = blendTopRow([section("a", ["a1"]), section("b", ["b1"])]);
    expect(out.blend).toBeNull();
  });

  it("round-robins one pick from each row in turn", () => {
    const out = blendTopRow([
      section("a", ["a1", "a2", "a3"]),
      section("b", ["b1", "b2", "b3"]),
    ]);
    expect(out.blend).not.toBeNull();
    const ids = out.blend!.albums.map((x) => x.id);
    expect(ids.slice(0, 4)).toEqual(["a1", "b1", "a2", "b2"]);
    expect(out.blend!.title).toBe("Recommended For You");
  });

  it("removes blended picks from source rows and drops rows left too thin", () => {
    const a = section(
      "a",
      Array.from({ length: 12 }, (_, i) => `a${i}`),
    );
    const b = section(
      "b",
      Array.from({ length: 12 }, (_, i) => `b${i}`),
    );
    const out = blendTopRow([a, b]);
    // 18 picked (9 from each), 3 left in each row.
    expect(out.blend!.albums).toHaveLength(18);
    const blended = new Set(out.blend!.albums.map((x) => x.id));
    for (const row of out.rows) {
      expect(row.albums.length).toBeGreaterThanOrEqual(3);
      expect(row.albums.every((x) => !blended.has(x.id))).toBe(true);
    }
  });

  it("shows a shared album only once, in the blend", () => {
    const out = blendTopRow([
      section("a", ["dup", "a2", "a3", "a4"]),
      section("b", ["dup", "b2", "b3", "b4"]),
    ]);
    const dupInBlend = out.blend!.albums.filter((x) => x.id === "dup").length;
    expect(dupInBlend).toBe(1);
    for (const row of out.rows) {
      expect(row.albums.some((x) => x.id === "dup")).toBe(false);
    }
  });
});
