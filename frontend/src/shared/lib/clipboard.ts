/**
 * Whether `navigator.clipboard.writeText` can actually be called here.
 *
 * The Clipboard API is gated on a secure context, so a dtxWiki served over
 * plain HTTP — the self-host path without TLS termination — leaves
 * `navigator.clipboard` undefined. Dereferencing it throws, and a caller that
 * swallows the throw shows nothing at all, which reads to the user as a dead
 * button they keep clicking.
 *
 * Lifted out of SettingsScreen, which has used this exact check since the copy
 * buttons were added there.
 */
export function canWriteToClipboard() {
  const secureContext =
    typeof window === "undefined" || window.isSecureContext !== false;
  return (
    secureContext &&
    typeof navigator !== "undefined" &&
    Boolean(navigator.clipboard?.writeText)
  );
}

/**
 * Select an element's contents so the user can copy by hand.
 *
 * The fallback for every environment `canWriteToClipboard` rules out: the
 * content is still reachable, it just takes Ctrl+C.
 */
export function selectText(target?: HTMLElement | null) {
  if (
    !target ||
    typeof window === "undefined" ||
    typeof document === "undefined" ||
    !document.createRange
  ) {
    return false;
  }
  const selection = window.getSelection();
  if (!selection) {
    return false;
  }
  const range = document.createRange();
  range.selectNodeContents(target);
  selection.removeAllRanges();
  selection.addRange(range);
  return true;
}
