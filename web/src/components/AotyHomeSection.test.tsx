import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("@/components/PlayMediaButton", () => ({
  PlayMediaButton: () => null,
}));

import { AotyHomeSection } from "./AotyHomeSection";
import { api } from "@/api/client";
import type { AotyAlbum, AotyPage, AotyStatus } from "@/api/types";

function album(id: string): AotyAlbum {
  return {
    title: `Album ${id}`,
    artist: "Artist",
    score: 80,
    rating_count: 100,
    cover_url: null,
    release_date: null,
    rank: null,
    must_hear: false,
    aoty_url: null,
    genres: [],
    genre_slugs: [],
    tidal_album: {
      id,
      name: `Album ${id}`,
      artists: [{ id: "a", name: "Artist" }],
    },
  } as unknown as AotyAlbum;
}

const status = (over: Partial<AotyStatus>): AotyStatus => ({
  blocked: false,
  solving: false,
  can_solve: true,
  issues_url: "",
  ...over,
});

const pendingPage: AotyPage = { items: [], has_more: false, pending: true };
const fullPage: AotyPage = {
  items: [album("1"), album("2")],
  has_more: false,
  pending: false,
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  // The rows size themselves from their measured width (useCardColumns),
  // and happy-dom does no layout, so give every element a desktop-ish
  // width or each row would fit zero cards.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 1200,
  } as DOMRect);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function mount() {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <AotyHomeSection />
      </MemoryRouter>,
    );
  });
  await settle();
}

describe("AotyHomeSection", () => {
  it("waits out a Cloudflare solve and then shows the rows", async () => {
    // The rows' own requests start the solve, so the first status says
    // nothing is happening; the rows come back pending.
    const statusSpy = vi
      .spyOn(api.aoty, "status")
      .mockResolvedValueOnce(status({}))
      .mockResolvedValueOnce(status({ solving: true }))
      .mockResolvedValue(status({}));
    const recent = vi
      .spyOn(api.aoty, "recentReleases")
      .mockResolvedValueOnce(pendingPage)
      .mockResolvedValue(fullPage);
    vi.spyOn(api.aoty, "topOfYear")
      .mockResolvedValueOnce(pendingPage)
      .mockResolvedValue(fullPage);

    await mount();
    expect(container.textContent).not.toContain("Album 1");
    expect(container.textContent).not.toContain("unavailable");

    // Still solving at the first check, done at the second.
    await settle(3000);
    expect(container.textContent).not.toContain("Album 1");
    await settle(3000);

    expect(recent).toHaveBeenCalledTimes(2);
    expect(statusSpy).toHaveBeenCalledTimes(4);
    expect(container.textContent).toContain("Album 1");
    expect(container.textContent).not.toContain("unavailable");
  });

  it("shows the notice when the solve fails", async () => {
    vi.spyOn(api.aoty, "status")
      .mockResolvedValueOnce(status({ solving: true }))
      .mockResolvedValue(status({ blocked: true }));
    vi.spyOn(api.aoty, "recentReleases")
      .mockResolvedValueOnce(pendingPage)
      .mockResolvedValue({ items: [], has_more: false, pending: false });
    vi.spyOn(api.aoty, "topOfYear")
      .mockResolvedValueOnce(pendingPage)
      .mockResolvedValue({ items: [], has_more: false, pending: false });

    await mount();
    expect(container.textContent).not.toContain("unavailable");

    await settle(3000);
    expect(container.textContent).toContain(
      "Album of the Year rows unavailable",
    );
  });
});
