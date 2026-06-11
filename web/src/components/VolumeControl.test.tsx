import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

// React 18 act() opt-in (same as the other component tests).
(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * Scroll-wheel volume (issue #195): a wheel tick over the volume
 * control nudges the volume by the configured step, Shift+scroll
 * steps 1 %, values clamp to [0, 1], and a disabled control
 * (Force Volume) ignores the wheel entirely.
 */

vi.mock("@/hooks/useAudioOptions", () => ({
  useAudioOptions: () => ({
    devices: [],
    current: "",
    exclusiveMode: false,
    forceVolume: false,
    volumeScrollStepPct: 5,
    loaded: true,
    setDevice: vi.fn(),
    setExclusiveMode: vi.fn(),
    setForceVolume: vi.fn(),
    setVolumeScrollStep: vi.fn(),
    refresh: vi.fn(),
  }),
}));

const { VolumeControl } = await import("./NowPlaying");

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function mount(props: {
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  await act(async () => {
    root.render(<VolumeControl {...props} />);
  });
}

function wheel(el: Element, init: WheelEventInit) {
  act(() => {
    const e = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ...init,
    });
    if (init.shiftKey) {
      // happy-dom's WheelEvent constructor drops the MouseEventInit
      // modifier keys; pin the property directly.
      Object.defineProperty(e, "shiftKey", { value: true });
    }
    el.dispatchEvent(e);
  });
}

describe("VolumeControl scroll wheel", () => {
  it("scroll up raises the volume by the configured step", async () => {
    const onChange = vi.fn();
    await mount({ value: 0.5, onChange });

    wheel(container.firstElementChild!, { deltaY: -100 });

    expect(onChange).toHaveBeenCalledWith(0.55);
  });

  it("scroll down lowers the volume by the configured step", async () => {
    const onChange = vi.fn();
    await mount({ value: 0.5, onChange });

    wheel(container.firstElementChild!, { deltaY: 100 });

    expect(onChange).toHaveBeenCalledWith(0.45);
  });

  it("shift+scroll steps by 1% for fine adjustment", async () => {
    const onChange = vi.fn();
    await mount({ value: 0.5, onChange });

    wheel(container.firstElementChild!, { deltaY: -100, shiftKey: true });

    expect(onChange).toHaveBeenCalledWith(0.51);
  });

  it("clamps at 100% and 0%", async () => {
    const onChange = vi.fn();
    await mount({ value: 0.98, onChange });
    wheel(container.firstElementChild!, { deltaY: -100 });
    expect(onChange).toHaveBeenCalledWith(1);

    onChange.mockClear();
    await act(async () => {
      root.render(<VolumeControl value={0.02} onChange={onChange} />);
    });
    wheel(container.firstElementChild!, { deltaY: 100 });
    expect(onChange).toHaveBeenCalledWith(0);
  });

  it("already at the boundary: no redundant onChange", async () => {
    const onChange = vi.fn();
    await mount({ value: 1, onChange });

    wheel(container.firstElementChild!, { deltaY: -100 });

    expect(onChange).not.toHaveBeenCalled();
  });

  it("ignores the wheel when disabled (Force Volume)", async () => {
    const onChange = vi.fn();
    await mount({ value: 0.5, onChange, disabled: true });

    wheel(container.firstElementChild!, { deltaY: -100 });

    expect(onChange).not.toHaveBeenCalled();
  });

  it("tracks the live value across successive ticks", async () => {
    // The listener installs once and reads through a ref — a second
    // tick after a re-render must step from the NEW value, not the
    // one captured at mount.
    const onChange = vi.fn();
    await mount({ value: 0.5, onChange });
    wheel(container.firstElementChild!, { deltaY: -100 });
    expect(onChange).toHaveBeenLastCalledWith(0.55);

    await act(async () => {
      root.render(<VolumeControl value={0.55} onChange={onChange} />);
    });
    wheel(container.firstElementChild!, { deltaY: -100 });
    expect(onChange).toHaveBeenLastCalledWith(0.6);
  });
});
