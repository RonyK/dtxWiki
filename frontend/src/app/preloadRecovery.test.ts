import { afterEach, describe, expect, it, vi } from "vitest";

import {
  preloadReloadKey,
  registerVitePreloadErrorRecovery,
} from "./preloadRecovery";

describe("vite preload recovery", () => {
  // Registrations are drained here, not at the end of each test: a failing
  // assertion returns before an inline unregister() call, leaking window
  // listeners into the next test and turning one real failure into a cascade
  // of unrelated ones.
  const registrations: Array<() => void> = [];

  const register = (options: Parameters<typeof registerVitePreloadErrorRecovery>[0]) => {
    const unregister = registerVitePreloadErrorRecovery(options);
    registrations.push(unregister);
    return unregister;
  };

  afterEach(() => {
    while (registrations.length > 0) registrations.pop()?.();
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("hard-reloads exactly once for the same entry hash and pathname", () => {
    const reload = vi.fn();
    window.history.replaceState(null, "", "/pages/guide");
    const unregister = register({
      entryUrl: "https://wiki.example/assets/index-entryHash123.js",
      reload,
    });

    const firstError = new CustomEvent("vite:preloadError", {
      cancelable: true,
      detail: new TypeError("Failed to fetch dynamically imported module"),
    });
    window.dispatchEvent(firstError);

    expect(reload).toHaveBeenCalledTimes(1);
    expect(firstError.defaultPrevented).toBe(true);
    expect(
      window.sessionStorage.getItem(
        preloadReloadKey("entryHash123", "/pages/guide"),
      ),
    ).toBe("1");

    const secondError = new CustomEvent("vite:preloadError", {
      cancelable: true,
      detail: new TypeError("Failed to fetch dynamically imported module"),
    });
    window.dispatchEvent(secondError);

    expect(reload).toHaveBeenCalledTimes(1);
    expect(secondError.defaultPrevented).toBe(false);
    unregister();
  });

  // E2E-01 regression. Under the packaged single-origin build the SPA is still
  // streaming lazily-imported chunks when the user navigates away; the browser
  // aborts them and Vite reports every abort as vite:preloadError. Reloading
  // there cancelled the navigation the user had just started and put them back
  // on the page they were leaving -- observed in Firefox and WebKit as
  // `page.goto` failing with NS_ERROR_FAILURE / "Frame load interrupted".
  it.each(["beforeunload", "pagehide"])(
    "ignores preload errors raised after %s, when the chunk was cancelled",
    (leaveEvent) => {
      const reload = vi.fn();
      window.history.replaceState(null, "", "/library");
      const unregister = register({
        entryUrl: "https://wiki.example/assets/index-entryHash123.js",
        reload,
      });

      window.dispatchEvent(new Event(leaveEvent));
      const abortedPreload = new CustomEvent("vite:preloadError", {
        cancelable: true,
        detail: new Error(
          "Unable to preload CSS for /assets/MarkdownView-BpTgdD6k.css",
        ),
      });
      window.dispatchEvent(abortedPreload);

      expect(reload).not.toHaveBeenCalled();
      expect(abortedPreload.defaultPrevented).toBe(false);
      expect(
        window.sessionStorage.getItem(
          preloadReloadKey("entryHash123", "/library"),
        ),
      ).toBeNull();
      unregister();
    },
  );

  it("recovers again after a back/forward cache round trip", () => {
    const reload = vi.fn();
    window.history.replaceState(null, "", "/library");
    const unregister = register({
      entryUrl: "https://wiki.example/assets/index-entryHash123.js",
      reload,
    });

    // Entering the bfcache fires pagehide with no unload; pageshow announces
    // the same document is live again, so recovery must come back with it.
    window.dispatchEvent(new Event("pagehide"));
    window.dispatchEvent(new Event("pageshow"));

    const staleChunk = new CustomEvent("vite:preloadError", {
      cancelable: true,
      detail: new TypeError("Failed to fetch dynamically imported module"),
    });
    window.dispatchEvent(staleChunk);

    expect(reload).toHaveBeenCalledTimes(1);
    expect(staleChunk.defaultPrevented).toBe(true);
    unregister();
  });

  it("still recovers when the document is not being left", () => {
    const reload = vi.fn();
    window.history.replaceState(null, "", "/library");
    const unregister = register({
      entryUrl: "https://wiki.example/assets/index-entryHash123.js",
      reload,
    });

    // A leave signal fired between two registrations must not reach this one.
    // (This does not pin the removeEventListener calls -- a stale listener
    // would set a flag on a dead closure that nobody reads. Listener symmetry
    // is pinned by the next test, which can observe it directly.)
    unregister();
    window.dispatchEvent(new Event("beforeunload"));

    const reregistered = register({
      entryUrl: "https://wiki.example/assets/index-entryHash123.js",
      reload,
    });
    const staleChunk = new CustomEvent("vite:preloadError", {
      cancelable: true,
      detail: new TypeError("Failed to fetch dynamically imported module"),
    });
    window.dispatchEvent(staleChunk);

    expect(reload).toHaveBeenCalledTimes(1);
    expect(staleChunk.defaultPrevented).toBe(true);
    reregistered();
  });

  it("unregister removes every listener it added", () => {
    // Deleting the three removeEventListener calls left the whole suite green,
    // because a stale listener only writes to a dead closure's flag. Observing
    // the target directly is the one place that difference is visible, and a
    // registration that outlives its component is a real leak in an SPA that
    // re-mounts the app root.
    const added: string[] = [];
    const removed: string[] = [];
    const target = {
      addEventListener: (type: string) => added.push(type),
      removeEventListener: (type: string) => removed.push(type),
    } as unknown as Window;

    const unregister = registerVitePreloadErrorRecovery({
      entryUrl: "https://wiki.example/assets/index-entryHash123.js",
      target,
      reload: vi.fn(),
    });
    expect(added).toContain("vite:preloadError");
    expect(added).toContain("beforeunload");
    expect(added).toContain("pagehide");
    expect(added).toContain("pageshow");

    unregister();

    expect([...removed].sort()).toEqual([...added].sort());
  });
});
