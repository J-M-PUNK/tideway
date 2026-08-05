import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act } from "react";
import type { ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

// The two dialogs render their own trees; the collapse layout under test
// only cares that the trigger shows in the expanded rail, so stub them to
// pass the trigger straight through.
vi.mock("@/components/CreatePlaylistDialog", () => ({
  CreatePlaylistDialog: ({ trigger }: { trigger: ReactNode }) => trigger,
}));
vi.mock("@/components/AddUrlDialog", () => ({
  AddUrlDialog: ({ trigger }: { trigger: ReactNode }) => trigger,
}));
vi.mock("@/hooks/useFeedUnread", () => ({ useFeedUnreadCount: () => 0 }));

import { Sidebar } from "./Sidebar";
import { UiPreferencesProvider } from "@/hooks/useUiPreferences";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  try {
    localStorage.removeItem("tideway:ui-prefs");
  } catch {
    /* ignore */
  }
  // The prefs provider's theme effect POSTs to a loopback endpoint on
  // mount; give it a no-op fetch so jsdom doesn't choke.
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }),
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount() {
  act(() => {
    root.render(
      <MemoryRouter>
        <UiPreferencesProvider>
          <Sidebar activeDownloads={0} newDownloads={0} />
        </UiPreferencesProvider>
      </MemoryRouter>,
    );
  });
}

function aside() {
  return container.querySelector("aside") as HTMLElement;
}

function toggle() {
  return container.querySelector(
    'button[aria-label="Collapse sidebar"], button[aria-label="Expand sidebar"]',
  ) as HTMLButtonElement;
}

function clickToggle() {
  act(() => {
    toggle().dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

it("starts expanded: wide rail, labels visible, collapse control", () => {
  mount();
  expect(aside().className).toContain("w-64");
  expect(aside().className).not.toContain("w-16");
  expect(container.textContent).toContain("Home");
  expect(container.textContent).toContain("Your Library");
  expect(container.textContent).toContain("Downloads");
  expect(toggle().getAttribute("aria-label")).toBe("Collapse sidebar");
});

it("collapsing narrows the rail and hides text labels", () => {
  mount();
  clickToggle();
  expect(aside().className).toContain("w-16");
  expect(aside().className).not.toContain("w-64");
  // Icon-only now — the text labels are gone.
  expect(container.textContent).not.toContain("Your Library");
  expect(container.textContent).not.toContain("Downloads");
  // The control flips to "expand".
  expect(toggle().getAttribute("aria-label")).toBe("Expand sidebar");
});

it("toggles back to expanded", () => {
  mount();
  clickToggle();
  clickToggle();
  expect(aside().className).toContain("w-64");
  expect(container.textContent).toContain("Your Library");
});

it("persists the collapsed choice across mounts", () => {
  mount();
  clickToggle();
  act(() => root.unmount());

  // Fresh mount reads the persisted pref and starts collapsed.
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  mount();
  expect(aside().className).toContain("w-16");
  expect(container.textContent).not.toContain("Your Library");
});
