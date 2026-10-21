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

export function canCopyText() {
  return (
    canWriteToClipboard() ||
    (typeof document !== "undefined" && typeof document.execCommand === "function")
  );
}

/** Try both browser copy paths before asking the user to copy manually. */
export async function writeTextToClipboard(text: string): Promise<boolean> {
  if (canWriteToClipboard()) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permissions can reject the modern API even on HTTPS.
    }
  }
  return copyThroughTransientNode(text);
}

/** Plain HTTP lacks the modern API. Keep the credential carrier transient. */
function copyThroughTransientNode(text: string): boolean {
  if (typeof document === "undefined" || typeof document.execCommand !== "function") {
    return false;
  }
  const carrier = document.createElement("textarea");
  carrier.value = text;
  carrier.readOnly = true;
  carrier.tabIndex = -1;
  carrier.setAttribute("aria-hidden", "true");
  Object.assign(carrier.style, {
    position: "fixed", top: "0", opacity: "0", pointerEvents: "none",
  });
  const focused = document.activeElement instanceof HTMLElement
    ? document.activeElement : null;
  const selection = window.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange())
    : [];
  // A modal focus trap must not redirect focus away from the copy carrier.
  (focused?.closest('[role="dialog"]') ?? document.body).append(carrier);
  try {
    carrier.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    carrier.remove();
    focused?.focus({ preventScroll: true });
    if (selection) {
      selection.removeAllRanges();
      for (const range of ranges) selection.addRange(range);
    }
  }
}

/**
 * Select an element's contents so the user can copy by hand.
 *
 * Last resort after both programmatic copy paths fail.
 */
export function selectText(target?: HTMLElement | null) {
  if (
    !target ||
    !target.isConnected ||
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
  try {
    const range = document.createRange();
    range.selectNodeContents(target);
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  } catch {
    return false;
  }
}
