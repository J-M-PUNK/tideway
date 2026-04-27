import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

// React 18 nags when act() is used without this flag set on the
// global. happy-dom is fine, we just have to opt in explicitly.
(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * Verifies the visibility-driven refetch behavior added to fix the
 * "songs show saved after deleting them from another client" bug.
 *
 *  - Initial fetch on mount.
 *  - Refetch when the tab becomes visible after being hidden.
 *  - Skip refetch while a toggle mutation is in flight (otherwise an
 *    optimistic add/remove racing the snapshot would get clobbered).
 */

vi.mock("@/api/client", () => ({
  api: {
    favorites: {
      snapshot: vi.fn(),
      add: vi.fn(),
      remove: vi.fn(),
    },
  },
}));

vi.mock("@/components/toast", () => ({
  useToast: () => ({ show: vi.fn() }),
}));

const { api } = await import("@/api/client");
const { FavoritesProvider, useFavorites } = await import("./useFavorites");

const snapshotMock = api.favorites.snapshot as ReturnType<typeof vi.fn>;
const addMock = api.favorites.add as ReturnType<typeof vi.fn>;
const removeMock = api.favorites.remove as ReturnType<typeof vi.fn>;

function emptySnapshot() {
  return { tracks: [], albums: [], artists: [], playlists: [], mixes: [] };
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

interface ProbeHandle {
  toggle: (kind: "track", id: string) => Promise<void>;
}

function Probe({ onMount }: { onMount: (h: ProbeHandle) => void }) {
  const ctx = useFavorites();
  useEffect(() => {
    onMount({ toggle: ctx.toggle });
  }, [ctx, onMount]);
  return null;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  snapshotMock.mockReset();
  addMock.mockReset();
  removeMock.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  setVisibility("visible");
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function flush() {
  // Two ticks: one for the resolved promise, one for the setState that
  // follows. happy-dom + React 18 reliably settle within two awaits.
  await Promise.resolve();
  await Promise.resolve();
}

describe("FavoritesProvider visibility refetch", () => {
  it("fetches once on mount", async () => {
    snapshotMock.mockResolvedValue(emptySnapshot());
    await act(async () => {
      root.render(
        <FavoritesProvider>
          <Probe onMount={() => {}} />
        </FavoritesProvider>,
      );
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(1);
  });

  it("refetches when the tab becomes visible again", async () => {
    snapshotMock.mockResolvedValue(emptySnapshot());
    await act(async () => {
      root.render(
        <FavoritesProvider>
          <Probe onMount={() => {}} />
        </FavoritesProvider>,
      );
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      setVisibility("hidden");
      setVisibility("visible");
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(2);
  });

  it("skips refetch while a toggle mutation is in flight", async () => {
    snapshotMock.mockResolvedValue(emptySnapshot());
    // Hold the mutation promise open so we can fire visibility events
    // mid-flight.
    let releaseAdd: () => void = () => {};
    addMock.mockImplementation(
      () =>
        new Promise<{ ok: true }>((resolve) => {
          releaseAdd = () => resolve({ ok: true });
        }),
    );

    let probe: ProbeHandle | null = null;
    await act(async () => {
      root.render(
        <FavoritesProvider>
          <Probe
            onMount={(h) => {
              probe = h;
            }}
          />
        </FavoritesProvider>,
      );
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(1);
    expect(probe).not.toBeNull();

    // Start a toggle without awaiting — it stays in flight until we
    // call releaseAdd().
    let togglePromise!: Promise<void>;
    await act(async () => {
      togglePromise = probe!.toggle("track", "abc");
      await flush();
    });
    expect(addMock).toHaveBeenCalledTimes(1);

    // Visibility flip while mutation pending: must NOT refetch.
    await act(async () => {
      setVisibility("hidden");
      setVisibility("visible");
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(1);

    // Resolve the mutation; subsequent visibility flips should refetch.
    await act(async () => {
      releaseAdd();
      await togglePromise;
      await flush();
    });
    await act(async () => {
      setVisibility("hidden");
      setVisibility("visible");
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(2);
  });

  it("ignores visibility events that don't transition to visible", async () => {
    snapshotMock.mockResolvedValue(emptySnapshot());
    await act(async () => {
      root.render(
        <FavoritesProvider>
          <Probe onMount={() => {}} />
        </FavoritesProvider>,
      );
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      setVisibility("hidden");
      await flush();
    });
    expect(snapshotMock).toHaveBeenCalledTimes(1);
  });
});
