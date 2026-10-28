const PREFIX = "dtxwiki.reviewDraft.";

/** Unsubmitted review notes are tab/session scoped and never shared by actors. */
export function reviewDraftKey(actorId: string, workspaceId: string, reviewId: string) {
  return `${PREFIX}${actorId}:${workspaceId}:${reviewId}`;
}

export function readReviewDraft(key: string): string {
  try {
    return sessionStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

export function writeReviewDraft(key: string, value: string) {
  try {
    if (value) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch {
    // The in-memory draft remains usable when browser storage is unavailable.
  }
}

export function clearReviewDrafts() {
  try {
    const keys = Object.keys(sessionStorage).filter((key) => key.startsWith(PREFIX));
    keys.forEach((key) => sessionStorage.removeItem(key));
  } catch {
    // Private browsing/storage restrictions must not prevent signing out.
  }
}
