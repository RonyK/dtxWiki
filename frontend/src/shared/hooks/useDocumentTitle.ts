import { useEffect } from "react";

const APP_NAME = "dtxWiki";

/**
 * Name the browser tab after what is on screen.
 *
 * Falls back to the app name when no document is selected.
 *
 * Restores whatever was there on unmount, so a screen that sets a title cannot
 * leave it behind for the next one.
 */
export function useDocumentTitle(title: string | null, separator = " · ") {
  useEffect(() => {
    if (typeof document === "undefined") {
      return undefined;
    }
    const previous = document.title;
    document.title = title ? `${title}${separator}${APP_NAME}` : APP_NAME;
    return () => {
      document.title = previous;
    };
  }, [title, separator]);
}
