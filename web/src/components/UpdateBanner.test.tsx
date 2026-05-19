import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

import { UpdateBanner } from "./UpdateBanner";
import { api } from "@/api/client";
import { ToastProvider } from "@/components/toast";

function flush() {
  return new Promise<void>((resolve) => setTimeout(resolve, 0));
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  try {
    localStorage.removeItem("tideway:update-dismissed-version");
  } catch {
    /* ignore */
  }
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
  vi.restoreAllMocks();
});

async function mountWith(response: {
  available: boolean;
  current: string;
  latest: string | null;
  url: string | null;
  notes: string | null;
  kind?: "flatpak" | "installer";
}) {
  vi.spyOn(api, "updateCheck").mockResolvedValue(response);
  await act(async () => {
    root.render(
      <ToastProvider>
        <UpdateBanner />
      </ToastProvider>,
    );
  });
  await act(async () => {
    await flush();
  });
}

describe("UpdateBanner", () => {
  it("shows the 'Install now' button when kind is installer", async () => {
    await mountWith({
      available: true,
      current: "1.9.4",
      latest: "v1.9.5",
      url: "https://example/r/v1.9.5",
      notes: "n",
      kind: "installer",
    });
    expect(container.textContent).toContain("Install now");
    expect(container.textContent).not.toContain("flatpak update");
  });

  it("hides 'Install now' and shows the flatpak hint when kind is flatpak", async () => {
    await mountWith({
      available: true,
      current: "1.9.4",
      latest: "v1.9.5",
      url: "https://example/r/v1.9.5",
      notes: "n",
      kind: "flatpak",
    });
    expect(container.textContent).not.toContain("Install now");
    expect(container.textContent).toContain(
      "flatpak update --user com.tidaldownloader.Tideway",
    );
    // The release notes button still shows so users can read the changelog.
    expect(container.textContent).toContain("Release notes");
  });

  it("renders nothing when no update is available", async () => {
    await mountWith({
      available: false,
      current: "1.9.5",
      latest: "v1.9.5",
      url: null,
      notes: null,
      kind: "installer",
    });
    expect(container.textContent).toBe("");
  });
});
