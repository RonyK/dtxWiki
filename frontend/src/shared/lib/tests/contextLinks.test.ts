import { describe, expect, it } from "vitest";

import fixture from "../../../../../shared/fixtures/context_link_locators.json";
import { classifyContextLink, formatContextPageMarkdown, formatContextPageUrl } from "../contextLinks";

describe("Context link locators", () => {
  // Budget exception: each shared fixture row documents a distinct parser or
  // trust-boundary failure in reason. One collected case runs the same public
  // language-neutral contract as Python; UI tests cannot prove parser parity.
  it("matches the shared classification and formatting contract", () => {
    for (const item of fixture.cases) {
      const actual = classifyContextLink(item.input, {
        workspaceId: fixture.workspace_id,
        selfOrigins: fixture.self_origins,
      });
      expect({
        kind: actual.kind,
        workspace_id: actual.workspaceId,
        page_id: actual.pageId,
        fragment: actual.fragment,
        stable_url: actual.stableUrl,
      }, item.name).toEqual({
        kind: item.kind,
        workspace_id: item.workspace_id,
        page_id: item.page_id,
        fragment: item.fragment,
        stable_url: item.stable_url,
      });
    }
    for (const item of fixture.format_cases) {
      expect(formatContextPageUrl(item.workspace_id, item.page_id, item.fragment)).toBe(item.stable_url);
      expect(formatContextPageMarkdown(item.title, item.workspace_id, item.page_id, item.fragment)).toBe(item.link_markdown);
    }
  });
});
