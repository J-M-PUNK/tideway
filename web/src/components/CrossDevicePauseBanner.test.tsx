import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

import { CrossDevicePauseBanner } from "./CrossDevicePauseBanner";
import { api } from "@/api/client";

// Hoist the playerMeta object so the mock factory and the test
// bodies share a single reference. Vitest hoists vi.mock factories
// above imports, which would otherwise capture a stale binding.
const playerMetaRef: { value: { pausedByDevice: string | null } } = {
  value: { pausedByDevice: null },
};

vi.mock("@/hooks/PlayerContext", () => ({
  usePlayerMeta: () => playerMetaRef.value,
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
  vi.restoreAllMocks();
  playerMetaRef.value = { pausedByDevice: null };
});

describe("CrossDevicePauseBanner", () => {
  it("renders nothing when no cross-device pause is pending", () => {
    playerMetaRef.value = { pausedByDevice: null };
    act(() => {
      root.render(<CrossDevicePauseBanner />);
    });
    expect(container.textContent).toBe("");
    expect(container.querySelector('[role="status"]')).toBeNull();
  });

  it("renders the device name when paused_by_device is set", () => {
    playerMetaRef.value = { pausedByDevice: "iOS" };
    act(() => {
      root.render(<CrossDevicePauseBanner />);
    });
    expect(container.textContent).toContain("Paused");
    expect(container.textContent).toContain("iOS");
    expect(container.querySelector('[role="status"]')).not.toBeNull();
  });

  it("hits the dismiss endpoint when the X button is clicked", async () => {
    playerMetaRef.value = { pausedByDevice: "Desktop" };
    const spy = vi
      .spyOn(api.player, "dismissPauseReason")
      .mockResolvedValue({} as never);
    act(() => {
      root.render(<CrossDevicePauseBanner />);
    });
    const button = container.querySelector(
      'button[aria-label="Dismiss"]',
    ) as HTMLButtonElement | null;
    expect(button).not.toBeNull();
    act(() => {
      button?.click();
    });
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("swallows dismiss endpoint failures so the UI doesn't crash", async () => {
    playerMetaRef.value = { pausedByDevice: "Desktop" };
    vi.spyOn(api.player, "dismissPauseReason").mockRejectedValue(
      new Error("server unreachable"),
    );
    act(() => {
      root.render(<CrossDevicePauseBanner />);
    });
    const button = container.querySelector(
      'button[aria-label="Dismiss"]',
    ) as HTMLButtonElement | null;
    // Should not throw / no unhandled rejection visible to the test.
    act(() => {
      button?.click();
    });
    // Component still renders even after a failed dismiss; the
    // server-side state is unchanged so the next snapshot will
    // still carry the same pause reason.
    expect(container.querySelector('[role="status"]')).not.toBeNull();
  });
});
