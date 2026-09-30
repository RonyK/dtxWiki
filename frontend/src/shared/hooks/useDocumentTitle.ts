import { useEffect } from "react";

const APP_NAME = "dtxWiki";

/**
 * Name the browser tab after what is on screen.
 *
 * The app ships one static `<title>dtxWiki</title>` and never writes
 * `document.title`, so every destination shares one tab title, one history
 * entry name and one bookmark name -- twelve Settings sections included.
 *
 * Restores whatever was there on unmount, so a screen that sets a title cannot
 * leave it behind for the next one.
 */
export function useDocumentTitle(title: string | null) {
  useEffect(() => {
    if (typeof document === "undefined" || !title) {
      return undefined;
    }
    const previous = document.title;
    document.title = `${title} · ${APP_NAME}`;
    return () => {
      document.title = previous;
    };
  }, [title]);
}
