import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

// React 18 act() opt-in (same as the other component tests).
(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * Interaction tests for the manual parametric EQ editor: it renders
 * bands from the server, add/remove mutate the list and persist via
 * setEq, picking a preset applies the preset's bands, and saved
 * manual profiles load through setEq and save through their endpoint.
 */

const CONFIG = {
  filter_types: ["PK", "LSC", "HSC"] as const,
  freq_min: 20,
  freq_max: 20000,
  gain_abs_max: 24,
  q_min: 0.1,
  q_max: 10,
  max_bands: 32,
};

vi.mock("@/api/client", () => ({
  api: {
    player: {
      eq: vi.fn(),
      setEq: vi.fn(),
      setEqPreset: vi.fn(),
      createEqProfile: vi.fn(),
      updateEqProfile: vi.fn(),
      deleteEqProfile: vi.fn(),
    },
  },
}));

vi.mock("@/components/toast", () => ({
  useToast: () => ({ show: vi.fn() }),
}));

const { api } = await import("@/api/client");
const { ParametricEqEditor } = await import("./SettingsPage");

const eqMock = api.player.eq as ReturnType<typeof vi.fn>;
const setEqMock = api.player.setEq as ReturnType<typeof vi.fn>;
const setEqPresetMock = api.player.setEqPreset as ReturnType<typeof vi.fn>;
const createEqProfileMock = api.player.createEqProfile as ReturnType<
  typeof vi.fn
>;

function band(over: Record<string, unknown> = {}) {
  return { type: "PK", freq: 1000, gain: 3, q: 1, enabled: true, ...over };
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  eqMock.mockReset();
  setEqMock.mockReset();
  setEqPresetMock.mockReset();
  createEqProfileMock.mockReset();
  setEqMock.mockResolvedValue({
    ok: true,
    enabled: true,
    bands: [],
    preamp: null,
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function mount(
  props: {
    bypassed?: boolean;
    onToggleBypass?: () => void;
  } = {},
) {
  await act(async () => {
    root.render(<ParametricEqEditor {...props} />);
  });
  // Let the mount effect's fetch resolve.
  await act(async () => {
    await Promise.resolve();
  });
}

describe("ParametricEqEditor", () => {
  it("seeds the default layout when the user has no saved bands", async () => {
    const defaults = [
      band({ type: "LSC", freq: 105, gain: 0, q: 0.7 }),
      band({ freq: 1000, gain: 0 }),
      band({ type: "HSC", freq: 10000, gain: 0, q: 0.7 }),
    ];
    eqMock.mockResolvedValue({
      enabled: true,
      bands: [],
      preamp: null,
      config: CONFIG,
      default_bands: defaults,
      manual_profiles: [],
      manual_profiles_max: 50,
      presets: [],
    });
    await mount();

    // Default nodes render as rows, but nothing is persisted on load.
    expect(container.querySelectorAll("select").length).toBe(3);
    expect(setEqMock).not.toHaveBeenCalled();
  });

  it("renders a row per band from the server state", async () => {
    eqMock.mockResolvedValue({
      enabled: true,
      bands: [band(), band({ type: "LSC", freq: 80 })],
      preamp: null,
      config: CONFIG,
      default_bands: [],
      manual_profiles: [],
      manual_profiles_max: 50,
      presets: [],
    });
    await mount();

    const selects = container.querySelectorAll("select");
    expect(selects.length).toBe(2);
  });

  it("adds a band and persists the longer list via setEq", async () => {
    eqMock.mockResolvedValue({
      enabled: true,
      bands: [band()],
      preamp: null,
      config: CONFIG,
      default_bands: [],
      manual_profiles: [],
      manual_profiles_max: 50,
      presets: [],
    });
    await mount();

    const addBtn = [...container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Add band"),
    )!;
    await act(async () => {
      addBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(setEqMock).toHaveBeenCalledTimes(1);
    const [bandsArg] = setEqMock.mock.calls[0];
    expect(bandsArg).toHaveLength(2);
  });

  it("removes a band and persists the shorter list", async () => {
    eqMock.mockResolvedValue({
      enabled: true,
      bands: [band(), band({ freq: 5000 })],
      preamp: null,
      config: CONFIG,
      default_bands: [],
      manual_profiles: [],
      manual_profiles_max: 50,
      presets: [],
    });
    await mount();

    const removeBtn = [...container.querySelectorAll("button")].find(
      (b) => b.getAttribute("title") === "Remove band",
    )!;
    await act(async () => {
      removeBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(setEqMock).toHaveBeenCalledTimes(1);
    const [bandsArg] = setEqMock.mock.calls[0];
    expect(bandsArg).toHaveLength(1);
  });

  it("applies a preset's bands when a preset card is clicked", async () => {
    const presetBands = [band({ type: "LSC", freq: 60, gain: 6 })];
    eqMock.mockResolvedValue({
      enabled: true,
      bands: [],
      preamp: null,
      config: CONFIG,
      default_bands: [],
      manual_profiles: [],
      manual_profiles_max: 50,
      presets: [{ index: 1, name: "Bass Boost", bands: presetBands }],
    });
    setEqPresetMock.mockResolvedValue({
      ok: true,
      enabled: true,
      bands: presetBands,
    });
    await mount();

    const presetBtn = [...container.querySelectorAll("button")].find((b) =>
      b.getAttribute("title")?.includes("Bass Boost"),
    )!;
    await act(async () => {
      presetBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(setEqPresetMock).toHaveBeenCalledWith(1);
    // After applying, a row for the preset's single band renders.
    await act(async () => {
      await Promise.resolve();
    });
    expect(container.querySelectorAll("select").length).toBe(1);
  });

  it("double-clicking a node removes that band", async () => {
    eqMock.mockResolvedValue({
      enabled: true,
      bands: [band(), band({ freq: 5000 })],
      preamp: null,
      config: CONFIG,
      default_bands: [],
      manual_profiles: [],
      manual_profiles_max: 50,
      presets: [],
    });
    await mount();

    const nodes = container.querySelectorAll("svg circle");
    expect(nodes.length).toBe(2);
    await act(async () => {
      nodes[0].dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    });

    expect(setEqMock).toHaveBeenCalledTimes(1);
    const [bandsArg] = setEqMock.mock.calls[0];
    expect(bandsArg).toHaveLength(1);
  });

  it("shows a bypass button that calls onToggleBypass", async () => {
    const onToggleBypass = vi.fn();
    eqMock.mockResolvedValue({
      enabled: true,
      bands: [band()],
      preamp: null,
      config: CONFIG,
      default_bands: [],
      manual_profiles: [],
      manual_profiles_max: 50,
      presets: [],
    });
    await mount({ bypassed: false, onToggleBypass });

    const bypassBtn = [...container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Bypass A/B"),
    )!;
    await act(async () => {
      bypassBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onToggleBypass).toHaveBeenCalledTimes(1);
  });

  describe("manual profiles", () => {
    const bass = {
      id: "p1",
      name: "Bass",
      bands: [band({ type: "LSC", freq: 105, gain: 6, q: 0.7 })],
      preamp: -3,
    };

    function stateWith(over: Record<string, unknown> = {}) {
      return {
        enabled: true,
        bands: [band()],
        preamp: null,
        config: CONFIG,
        default_bands: [],
        presets: [],
        manual_profiles: [bass],
        manual_profiles_max: 50,
        ...over,
      };
    }

    const card = (name: string) =>
      [...container.querySelectorAll("button")].find(
        (b) => b.getAttribute("title") === `Load profile "${name}"`,
      )!;

    // Radix renders the dialog in a portal on document.body.
    const dialogInput = () =>
      document.body.querySelector<HTMLInputElement>(
        'input[aria-label="Profile name"]',
      );

    async function typeName(value: string) {
      const input = dialogInput()!;
      const setValue = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!;
      await act(async () => {
        setValue.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }

    async function openSaveDialog() {
      const saveBtn = [...container.querySelectorAll("button")].find((b) =>
        b.textContent?.includes("Save current"),
      )!;
      await act(async () => {
        saveBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
    }

    async function submitDialog() {
      await act(async () => {
        dialogInput()!
          .form!.querySelector<HTMLButtonElement>('button[type="submit"]')!
          .click();
      });
      await act(async () => {
        await Promise.resolve();
      });
    }

    it("loads a profile's curve through setEq", async () => {
      eqMock.mockResolvedValue(stateWith());
      await mount();

      expect(card("Bass").getAttribute("aria-pressed")).toBe("false");
      await act(async () => {
        card("Bass").dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });

      expect(setEqMock).toHaveBeenCalledWith(bass.bands, -3);
      expect(card("Bass").getAttribute("aria-pressed")).toBe("true");
    });

    it("marks the profile that matches the curve on load", async () => {
      eqMock.mockResolvedValue(stateWith({ bands: bass.bands, preamp: -3 }));
      await mount();
      expect(card("Bass").getAttribute("aria-pressed")).toBe("true");
    });

    it("does not mark a profile whose preamp differs", async () => {
      eqMock.mockResolvedValue(stateWith({ bands: bass.bands, preamp: 0 }));
      await mount();
      expect(card("Bass").getAttribute("aria-pressed")).toBe("false");
    });

    it("saves the current curve under the typed name", async () => {
      const saved = { id: "p2", name: "Mine", bands: [band()], preamp: null };
      eqMock.mockResolvedValue(stateWith());
      createEqProfileMock.mockResolvedValue({
        profile: saved,
        profiles: [bass, saved],
      });
      await mount();

      await openSaveDialog();
      await typeName("  Mine ");
      await submitDialog();

      expect(createEqProfileMock).toHaveBeenCalledWith("Mine", [band()], null);
      expect(dialogInput()).toBeNull();
      expect(card("Mine")).toBeTruthy();
    });

    it("keeps the dialog open with the server's reason on a clash", async () => {
      eqMock.mockResolvedValue(stateWith());
      createEqProfileMock.mockRejectedValue(
        new Error(
          '409: {"detail":"A profile named \\"Bass\\" already exists"}',
        ),
      );
      await mount();

      await openSaveDialog();
      await typeName("bass");
      await submitDialog();

      expect(dialogInput()).not.toBeNull();
      expect(document.body.textContent).toContain(
        'A profile named "Bass" already exists',
      );
    });

    it("disables saving at the server's cap", async () => {
      eqMock.mockResolvedValue(stateWith({ manual_profiles_max: 1 }));
      await mount();
      const saveBtn = [...container.querySelectorAll("button")].find((b) =>
        b.textContent?.includes("Save current"),
      )!;
      expect(saveBtn.disabled).toBe(true);
    });
  });
});
