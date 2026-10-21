import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "./client";
import { outgoingContentError } from "./inputPolicy";

afterEach(() => vi.restoreAllMocks());

describe("outgoing content limits", () => {
  it("rejects an oversized content write before fetch", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    await expect(apiRequest("/api/v1/workspaces/w/context/pages", {
      method: "POST", body: JSON.stringify({ title: "😀".repeat(257) }),
    })).rejects.toMatchObject({ status: 422 });
    expect(fetch).not.toHaveBeenCalled();
  });
  it("keeps endpoint-specific title and comment budgets", () => {
    expect(outgoingContentError("/api/v1/workspaces", JSON.stringify({ title: "x".repeat(129) }))).not.toBe("");
    expect(outgoingContentError("/api/v1/workspaces/w/mcp-chats/c", JSON.stringify({ title: "x".repeat(121) }))).not.toBe("");
    expect(outgoingContentError("/api/v1/workspaces/w/context/pages/p/comments", JSON.stringify({ note: "x".repeat(5000) }))).toBe("");
    expect(outgoingContentError("/api/v1/workspaces/w/reviews/r", JSON.stringify({ decision_reason: "x".repeat(2001) }))).not.toBe("");
    expect(outgoingContentError("/api/v1/workspaces/w/ask/sessions/s/turns", JSON.stringify({ user_message: ` ${"x".repeat(8000)} ` }))).toBe("");
    expect(outgoingContentError("/api/v1/workspaces/w/ask/sessions/s/turns", JSON.stringify({ user_message: `\u0000${"x".repeat(8000)}` }))).not.toBe("");
  });
  it("keeps the semantic editor's UTF-8 byte budget", () => {
    expect(outgoingContentError("/api/v1/workspaces/w/library/versions/v/semantic-markdown", JSON.stringify({ body_markdown: "가".repeat(333334) }))).not.toBe("");
  });
  it("preserves partial credential edits and does not treat passwords as content", () => {
    expect(outgoingContentError("/api/v1/auth/tokens/t", JSON.stringify({ name: "Key" }))).toBe("");
    expect(outgoingContentError("/api/v1/auth/login", JSON.stringify({ password: "x".repeat(129) }))).toBe("");
  });
});
