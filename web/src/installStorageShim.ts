/**
 * Guarantee `window.localStorage` exists and works before anything
 * else runs.
 *
 * WebKitGTK (the Linux backend pywebview uses) can leave
 * `localStorage` absent or throwing — pywebview opens the webview in
 * private mode by default, and after a long session the WebKitGTK web
 * process can crash and reload into a context where the storage
 * binding is gone. The very first bare `localStorage.getItem(...)`
 * then throws a ReferenceError ("Can't find variable: localStorage"),
 * which the React error boundary catches and blanks the whole UI —
 * exactly the crash reported in the Linux issue. WKWebView on macOS
 * has a milder version of the same private-mode storage flakiness.
 *
 * This installs an in-memory `Storage` whenever the real one is
 * unavailable, so every existing `localStorage.*` call across the app
 * keeps working. Persistence degrades to the session in that state
 * (player position and prefs won't survive a reload), but the UI
 * stays alive instead of dying. When real localStorage works, this is
 * a no-op and the native store is used unchanged.
 *
 * Imported first in main.tsx so it runs before any component renders.
 */

function storageWorks(s: Storage | null | undefined): s is Storage {
  if (!s) return false;
  try {
    const probe = "__tideway_ls_probe__";
    s.setItem(probe, "1");
    s.removeItem(probe);
    return true;
  } catch {
    // Present but throwing (quota, disabled, sandboxed) — treat as
    // unusable so the caller installs the in-memory fallback.
    return false;
  }
}

function createMemoryStorage(): Storage {
  let mem: Record<string, string> = Object.create(null);
  return {
    get length() {
      return Object.keys(mem).length;
    },
    clear() {
      mem = Object.create(null);
    },
    getItem(key: string): string | null {
      const k = String(key);
      return k in mem ? mem[k] : null;
    },
    key(index: number): string | null {
      const keys = Object.keys(mem);
      return index >= 0 && index < keys.length ? keys[index] : null;
    },
    removeItem(key: string) {
      delete mem[String(key)];
    },
    setItem(key: string, value: string) {
      mem[String(key)] = String(value);
    },
  } as Storage;
}

export function installLocalStorageFallback(): boolean {
  let real: Storage | null = null;
  try {
    real = window.localStorage;
  } catch {
    // The `localStorage` getter itself can throw a SecurityError in
    // locked-down WebViews — swallow and fall through to the shim.
    real = null;
  }
  if (storageWorks(real)) return false;

  const shim = createMemoryStorage();
  try {
    Object.defineProperty(window, "localStorage", {
      value: shim,
      configurable: true,
      writable: true,
    });
  } catch {
    try {
      (window as unknown as { localStorage: Storage }).localStorage = shim;
    } catch {
      // Non-configurable, non-writable accessor that also throws —
      // nothing more we can do; callers still crash, but this is the
      // pathological case the WebKitGTK report is not.
      return false;
    }
  }
  return true;
}

installLocalStorageFallback();
