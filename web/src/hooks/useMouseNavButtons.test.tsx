import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

const navigate = vi.fn();
vi.mock("react-router-dom", () => ({ useNavigate: () => navigate }));

import { useMouseNavButtons } from "./useMouseNavButtons";

function Harness() {
  useMouseNavButtons();
  return null;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  navigate.mockClear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<Harness />));
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
});

function mouseup(button: number) {
  act(() => {
    window.dispatchEvent(new MouseEvent("mouseup", { button }));
  });
}

it("back side button (3) navigates back", () => {
  mouseup(3);
  expect(navigate).toHaveBeenCalledWith(-1);
});

it("forward side button (4) navigates forward", () => {
  mouseup(4);
  expect(navigate).toHaveBeenCalledWith(1);
});

it("primary/middle/right clicks are ignored", () => {
  mouseup(0);
  mouseup(1);
  mouseup(2);
  expect(navigate).not.toHaveBeenCalled();
});
