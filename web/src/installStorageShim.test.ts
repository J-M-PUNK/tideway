import { afterEach, describe, expect, it } from "vitest";
import { installLocalStorageFallback } from "./installStorageShim";

/**
 * The Linux "Can't find variable: localStorage" crash: WebKitGTK
 * leaves localStorage absent or throwing, and the first bare access
 * blanks the UI via the error boundary. The shim must make
 * localStorage usable again so existing call sites don't throw.
 */

const realDescriptor = Object.getOwnPropertyDescriptor(window, "localStorage");

afterEach(() => {
  // Restore the genuine localStorage between cases.
  if (realDescriptor) {
    Object.defineProperty(window, "localStorage", realDescriptor);
  }
  try {
    window.localStorage.clear();
  } catch {
    /* ignore */
  }
});

function removeLocalStorage() {
  Object.defineProperty(window, "localStorage", {
    value: undefined,
    configurable: true,
    writable: true,
  });
}

function makeLocalStorageThrow() {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    get() {
      throw new Error("SecurityError");
    },
  });
}

describe("installLocalStorageFallback", () => {
  it("is a no-op when real localStorage works", () => {
    const installed = installLocalStorageFallback();
    expect(installed).toBe(false);
    // Real store still functions.
    window.localStorage.setItem("k", "v");
    expect(window.localStorage.getItem("k")).toBe("v");
  });

  it("installs an in-memory store when localStorage is absent", () => {
    removeLocalStorage();
    const installed = installLocalStorageFallback();
    expect(installed).toBe(true);

    // The classic crash: a bare reference must now resolve.
    expect(() => localStorage.getItem("missing")).not.toThrow();
    localStorage.setItem("theme", "dark");
    expect(localStorage.getItem("theme")).toBe("dark");
    localStorage.removeItem("theme");
    expect(localStorage.getItem("theme")).toBeNull();
  });

  it("installs a fallback when the localStorage getter throws", () => {
    makeLocalStorageThrow();
    const installed = installLocalStorageFallback();
    expect(installed).toBe(true);
    expect(() => window.localStorage.setItem("a", "b")).not.toThrow();
    expect(window.localStorage.getItem("a")).toBe("b");
  });

  it("the in-memory store implements the Storage surface", () => {
    removeLocalStorage();
    installLocalStorageFallback();
    const ls = window.localStorage;
    ls.clear();
    expect(ls.length).toBe(0);
    ls.setItem("one", "1");
    ls.setItem("two", "2");
    expect(ls.length).toBe(2);
    expect(ls.key(0)).toBe("one");
    expect(ls.key(99)).toBeNull();
    ls.clear();
    expect(ls.length).toBe(0);
    expect(ls.getItem("one")).toBeNull();
  });

  it("coerces non-string keys and values like the real Storage", () => {
    removeLocalStorage();
    installLocalStorageFallback();
    const ls = window.localStorage;
    // Real Storage stringifies both; the shim must match so callers
    // that pass a number don't get surprising behavior.
    ls.setItem(1 as unknown as string, 2 as unknown as string);
    expect(ls.getItem("1")).toBe("2");
  });
});
