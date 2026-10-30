import { describe, expect, it } from "vitest";

import {
  classifyContextLink,
  formatContextPageMarkdown,
  formatContextPageUrl,
} from "../contextLinks";

const workspaceId = "11111111-1111-4111-8111-111111111111";
const pageId = "22222222-2222-4222-8222-222222222222";

describe("Context link locators", () => {
  it("inherits explicit source identity for short links and preserves stable identities", () => {
    const href = `/p/${workspaceId}/${pageId}#details`;
    expect(classifyContextLink(`/p/${pageId}#details`, { workspaceId })).toMatchObject({
      kind: "stable_short", pageId, stableUrl: href,
    });
    expect(classifyContextLink(`/p/${pageId}#details`).stableUrl).toBeNull();
    expect(classifyContextLink(`/workspaces/${workspaceId}/context/pages/${pageId}/edit#details`))
      .toMatchObject({ kind: "canonical_route", workspaceId, pageId, stableUrl: href });
    expect(classifyContextLink(`https://wiki.example${href}`, { selfOrigins: ["https://wiki.example"] }))
      .toMatchObject({ kind: "stable_full", workspaceId, pageId, stableUrl: href });
    expect(classifyContextLink(`https://other.example${href}`, { selfOrigins: ["https://wiki.example"] }).kind)
      .toBe("external");
    expect(formatContextPageUrl(workspaceId, pageId, "details")).toBe(href);
    expect(formatContextPageMarkdown("Notes [draft]", workspaceId, pageId, "details"))
      .toBe(`[Notes \\[draft\\]](${href})`);
  });

  it("accepts only explicit typed workspace navigation when the reader opts in", () => {
    const valid = `/workspaces/${workspaceId}/library/items/${pageId}?page=2`;
    expect(classifyContextLink(valid, { allowWorkspaceNavigation: true })).toMatchObject({
      kind: "workspace_navigation", stableUrl: valid,
    });
    expect(classifyContextLink(valid).kind).toBe("unknown");
    expect(classifyContextLink(
      `/workspaces/${workspaceId}/library/items/${pageId}?target=other`,
      { allowWorkspaceNavigation: true },
    ).kind).toBe("unknown");
    expect(classifyContextLink(
      `/workspaces/${workspaceId}/library/items/not-an-id`,
      { allowWorkspaceNavigation: true },
    ).kind).toBe("unknown");
    const inbox = `/workspaces/${workspaceId}/inbox`;
    expect(classifyContextLink(inbox, { allowWorkspaceNavigation: true })).toMatchObject({
      kind: "workspace_navigation", stableUrl: inbox,
    });
    expect(classifyContextLink(inbox).kind).toBe("unknown");
  });

  it("keeps authored opaque schemes external and rejects normalization into trusted routes", () => {
    expect(classifyContextLink("s://service/reference").kind).toBe("external");
    expect(classifyContextLink("node:/context/notes").kind).toBe("external");
    expect(classifyContextLink(`https://wiki.example/a/../p/${workspaceId}/${pageId}`, {
      selfOrigins: ["https://wiki.example"],
    }).kind).toBe("unknown");
    expect(classifyContextLink(`/p/${workspaceId}/%2e%2e/${pageId}`).kind).toBe("unknown");
    expect(classifyContextLink(`//wiki.example/p/${workspaceId}/${pageId}`).kind).toBe("unknown");
    expect(classifyContextLink(`dtxwiki://index/context/${pageId}`).kind).toBe("unknown");
  });
});
