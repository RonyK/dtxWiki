export type ContextPublishNotice =
  | { kind: "published" }
  | { kind: "published_with_findings" }
  | { kind: "review_completed" }
  | { kind: "review_failed"; error: string }
  | { kind: "review_superseded" };

export type ContextPublishNavigationState = {
  contextPublishNotice?: ContextPublishNotice;
};

export function readContextPublishNotice(state: unknown): ContextPublishNotice | null {
  if (!state || typeof state !== "object") {
    return null;
  }
  const notice = (state as ContextPublishNavigationState).contextPublishNotice;
  if (!notice || typeof notice !== "object" || !("kind" in notice)) {
    return null;
  }
  switch (notice.kind) {
    case "published":
    case "published_with_findings":
    case "review_completed":
    case "review_superseded":
      return notice;
    case "review_failed":
      return typeof notice.error === "string" ? notice : null;
    default:
      return null;
  }
}
