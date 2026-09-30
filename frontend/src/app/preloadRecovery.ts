const PRELOAD_RELOAD_STORAGE_PREFIX = "dtxwiki.preloadReload";

type PreloadRecoveryOptions = {
  entryUrl?: string;
  pathname?: () => string;
  reload?: () => void;
  storage?: Storage;
  target?: Window;
};

export function registerVitePreloadErrorRecovery(
  options: PreloadRecoveryOptions = {},
) {
  const target = options.target ?? window;
  const storage = options.storage ?? window.sessionStorage;
  const pathname = options.pathname ?? (() => window.location.pathname);
  const reload = options.reload ?? (() => window.location.reload());
  const entryHash = entryHashFromUrl(options.entryUrl ?? import.meta.url);

  // Leaving this document aborts every module and stylesheet preload it still
  // has in flight, and Vite reports each abort as a vite:preloadError -- the
  // same event a genuinely missing chunk raises. Reloading for one of those
  // would cancel the navigation the user just started and drop them back on
  // the page they were leaving. Firefox and WebKit both raise the abort while
  // the old document is still running its event loop, so the recovery has to
  // ask whether this document is still the one the user is on. beforeunload
  // and pagehide are the two signals that fire before those aborts.
  //
  // pagehide also fires when the document merely enters the back/forward
  // cache, so pageshow has to clear the flag again -- otherwise one bfcache
  // round trip would disable recovery for the rest of the document's life.
  // A navigation that fires beforeunload and is then cancelled (a download, a
  // mailto: link) does leave the flag set with no pageshow to clear it; the
  // cost is that a later stale-chunk error falls through to the router's root
  // boundary and its "Load the latest version" button instead of reloading by
  // itself, which is a degraded recovery rather than a broken one.
  let leavingDocument = false;
  const markLeavingDocument = () => {
    leavingDocument = true;
  };
  const markDocumentShown = () => {
    leavingDocument = false;
  };
  target.addEventListener("beforeunload", markLeavingDocument);
  target.addEventListener("pagehide", markLeavingDocument);
  target.addEventListener("pageshow", markDocumentShown);

  const handlePreloadError = (event: Event) => {
    if (leavingDocument) {
      // Not a recoverable failure: the chunk was cancelled, not missing. Leave
      // the event un-prevented so nothing else mistakes it for handled.
      return;
    }
    const key = preloadReloadKey(entryHash, pathname());
    try {
      if (storage.getItem(key) !== null) {
        // Do not prevent the second error: React Router's eager root boundary
        // must receive it instead of entering a reload loop.
        return;
      }
      storage.setItem(key, "1");
    } catch {
      // Without durable session storage there is no safe cross-reload guard.
      // Surface the original error to the router boundary.
      return;
    }

    event.preventDefault();
    reload();
  };

  target.addEventListener("vite:preloadError", handlePreloadError);
  return () => {
    target.removeEventListener("vite:preloadError", handlePreloadError);
    target.removeEventListener("beforeunload", markLeavingDocument);
    target.removeEventListener("pagehide", markLeavingDocument);
    target.removeEventListener("pageshow", markDocumentShown);
  };
}

export function entryHashFromUrl(entryUrl: string) {
  const pathname = new URL(entryUrl, window.location.href).pathname;
  const filename = pathname.split("/").pop() ?? "entry";
  const stem = filename.replace(/\.[^.]+$/, "");
  const match = stem.match(/-([A-Za-z0-9_-]+)$/);
  return match?.[1] ?? stem;
}

export function preloadReloadKey(entryHash: string, pathname: string) {
  return [
    PRELOAD_RELOAD_STORAGE_PREFIX,
    encodeURIComponent(entryHash),
    encodeURIComponent(pathname),
  ].join(":");
}
