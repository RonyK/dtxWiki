import { describe, expect, it } from "vitest";

import type { RankedSearchResult } from "../api/operations";
import { searchResultHref, searchResultLink } from "./searchResultHref";

describe("searchResultHref", () => {
  it("uses content chunk document ids for semantic context hits", () => {
    expect(
      searchResultHref(
        result("chunk-1", "context_page", {
          kind: "content_chunk",
          document_id: "page-1",
        }),
      ),
    ).toBe("/context?target=page-1");
  });

  it("opens library artifacts on their parent library item", () => {
    expect(
      searchResultHref(
        result(
          "artifact-1",
          "library_artifact",
          { kind: "library_artifact", id: "artifact-1" },
          "/library/item-1.md",
        ),
      ),
    ).toBe("/library?target=item-1");
  });

  it("does not expose a broken library artifact link without a parent item", () => {
    expect(
      searchResultHref(
        result("artifact-1", "library_artifact", {
          kind: "library_artifact",
          id: "artifact-1",
        }),
      ),
    ).toBeNull();
    expect(
      searchResultLink(result("artifact-1", "library_artifact")),
    ).toBeNull();
  });

  it("opens graph citation versions on their parent library item", () => {
    expect(
      searchResultHref({
        id: "version-1",
        type: "library_item_version",
        concept_path: "/library/item-1.md",
        source_ref: { kind: "library_item_version", id: "version-1" },
      }),
    ).toBe("/library?target=item-1");
  });

  it("opens context claim hits on their parent context page", () => {
    expect(
      searchResultHref(
        result("claim-1", "context_claim", {
          kind: "context_page",
          id: "page-1",
        }),
      ),
    ).toBe("/context?target=page-1");
  });

  it("opens feedback hits on their target document when provided", () => {
    expect(
      searchResultHref(
        result("feedback-1", "feedback", {
          kind: "context_page",
          id: "page-1",
        }),
      ),
    ).toBe("/context?target=page-1");
    expect(
      searchResultHref(
        result("feedback-2", "feedback", {
          kind: "library_item",
          id: "item-1",
        }),
      ),
    ).toBe("/library?target=item-1");
    expect(
      searchResultHref(
        result("feedback-3", "feedback", {
          kind: "agent_page",
          id: "agent-1",
        }),
      ),
    ).toBe("/agent?target=agent-1");
    for (const [kind, id, href, destination] of [
      ["context_page", "page-1", "/context?target=page-1", "context"],
      ["library_item", "item-1", "/library?target=item-1", "library"],
      ["agent_page", "agent-1", "/agent?target=agent-1", "agent"],
    ]) {
      expect(
        searchResultLink(result("feedback-1", "feedback", { kind, id })),
      ).toEqual({ href, destination });
    }
    expect(searchResultLink(result("feedback-1", "feedback"))).toBeNull();
  });

  it("opens change set hits only when a review item is known", () => {
    expect(
      searchResultHref(
        result("change-1", "change_set", {
          kind: "review_item",
          id: "review-1",
        }),
      ),
    ).toBe("/review?review=review-1");
    expect(searchResultHref(result("change-2", "change_set"))).toBeNull();
    expect(
      searchResultLink(result("change-1", "change_set", {
        kind: "review_item", id: "review-1",
      })),
    ).toEqual({ href: "/review?review=review-1", destination: "review" });
    expect(searchResultLink(result("change-2", "change_set"))).toBeNull();
  });

  it("keeps legacy source hits as library links", () => {
    expect(searchResultHref(result("source-1", "source"))).toBe(
      "/library?target=source-1",
    );
  });

  // P10.1: an explicit workspace id switches context_page/context_claim (and
  // the feedback-on-a-context-page case) to the canonical permalink. Every
  // other target type is unaffected — folders and non-Context surfaces have
  // no canonical shape to switch to.
  describe("with a workspace id", () => {
    it("builds the canonical Context permalink for a context page hit", () => {
      expect(
        searchResultHref(
          result("chunk-1", "context_page", {
            kind: "content_chunk",
            document_id: "page-1",
          }),
          "workspace-1",
        ),
      ).toBe("/workspaces/workspace-1/context/pages/page-1");
      expect(
        searchResultLink(
          result("chunk-1", "context_page", {
            kind: "content_chunk", document_id: "page-1",
          }),
          "workspace-1",
        ),
      ).toEqual({
        href: "/workspaces/workspace-1/context/pages/page-1",
        destination: "context",
      });
    });

    it("builds the canonical Context permalink for a context claim hit", () => {
      expect(
        searchResultHref(
          result("claim-1", "context_claim", {
            kind: "context_page",
            id: "page-1",
          }),
          "workspace-1",
        ),
      ).toBe("/workspaces/workspace-1/context/pages/page-1");
      expect(
        searchResultLink(result("claim-1", "context_claim"), "workspace-1"),
      ).toBeNull();
    });

    it("builds the canonical Context permalink for a feedback hit on a context page", () => {
      expect(
        searchResultHref(
          result("feedback-1", "feedback", {
            kind: "context_page",
            id: "page-1",
          }),
          "workspace-1",
        ),
      ).toBe("/workspaces/workspace-1/context/pages/page-1");
    });

    it("leaves non-Context target types on their existing query-param link", () => {
      expect(
        searchResultHref(
          result(
            "artifact-1",
            "library_artifact",
            { kind: "library_artifact", id: "artifact-1" },
            "/library/item-1.md",
          ),
          "workspace-1",
        ),
      ).toBe("/library?target=item-1");
    });
  });
});

function result(
  id: string,
  type: RankedSearchResult["type"],
  sourceRef?: Record<string, string>,
  conceptPath?: string,
): RankedSearchResult {
  return {
    id,
    type,
    title: "Hit",
    snippet: "Snippet",
    score: 1,
    citation_available: true,
    stale: false,
    archived: false,
    source_ref: sourceRef,
    concept_path: conceptPath,
  };
}
