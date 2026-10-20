export type CopyTextResult = "copied" | "selected" | "unavailable";
export type CopyTextReason = "permission" | "insecure" | "unsupported" | "selection";
export type CopyTextDetailedResult = {
  status: CopyTextResult;
  reason?: CopyTextReason;
};

/**
 * Copy text without reporting a false success. When the async clipboard API is
 * unavailable (notably on plain HTTP) or rejects, select the visible fallback
 * text so the user can still press Ctrl+C.
 */
export async function copyTextWithFallback(
  text: string,
  fallbackTarget?: HTMLElement | null,
): Promise<CopyTextResult> {
  return (await copyTextWithReason(text, fallbackTarget)).status;
}

export async function copyTextWithReason(
  text: string,
  fallbackTarget?: HTMLElement | null | (() => HTMLElement | null),
): Promise<CopyTextDetailedResult> {
  const clipboardAvailable = canWriteToClipboard();
  let reason: CopyTextReason =
    typeof window !== "undefined" && window.isSecureContext === false
      ? "insecure"
      : "unsupported";
  if (clipboardAvailable) {
    try {
      await navigator.clipboard.writeText(text);
      return { status: "copied" };
    } catch {
      reason = "permission";
      // Permission can still be denied in a secure context. Continue to the
      // explicit-selection fallback instead of claiming a copy succeeded.
    }
  }
  const target = typeof fallbackTarget === "function" ? fallbackTarget() : fallbackTarget;
  if (selectText(target)) {
    return { status: "selected", reason };
  }
  // A caller without visible fallback text (for example a tree context menu)
  // should hear why automatic clipboard access failed. When a visible target
  // existed but could not be selected, selection itself is the actionable
  // failure instead.
  return {
    status: "unavailable",
    reason: target ? "selection" : reason,
  };
}

function canWriteToClipboard() {
  const secureContext =
    typeof window === "undefined" || window.isSecureContext !== false;
  return (
    secureContext &&
    typeof navigator !== "undefined" &&
    Boolean(navigator.clipboard?.writeText)
  );
}

function selectText(target?: HTMLElement | null) {
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
