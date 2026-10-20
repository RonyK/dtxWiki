import { canWriteToClipboard, selectText, writeTextToClipboard } from "./clipboard";

export type CopyTextResult = "copied" | "selected" | "unavailable";
export type CopyTextReason = "permission" | "insecure" | "unsupported" | "selection";
export type CopyTextDetailedResult = {
  status: CopyTextResult;
  reason?: CopyTextReason;
};

/**
 * Copy text without reporting a false success. When the async clipboard API is
 * unavailable or rejects, try the legacy copy command, then select the visible fallback
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
  fallbackTarget?: HTMLElement | null,
): Promise<CopyTextDetailedResult> {
  const clipboardAvailable = canWriteToClipboard();
  let reason: CopyTextReason =
    typeof window !== "undefined" && window.isSecureContext === false
      ? "insecure"
      : "unsupported";
  if (await writeTextToClipboard(text)) return { status: "copied" };
  if (clipboardAvailable) reason = "permission";
  if (selectText(fallbackTarget)) {
    return { status: "selected", reason };
  }
  // A caller without visible fallback text (for example a tree context menu)
  // should hear why automatic clipboard access failed. When a visible target
  // existed but could not be selected, selection itself is the actionable
  // failure instead.
  return {
    status: "unavailable",
    reason: fallbackTarget ? "selection" : reason,
  };
}
