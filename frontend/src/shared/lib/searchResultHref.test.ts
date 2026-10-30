import { describe, expect, it } from "vitest";

import type { RankedSearchResult } from "../api/operations";
import { searchResultHref, searchResultLink } from "./searchResultHref";

describe("searchResultHref", () => {
  it("uses content chunk document ids for semantic context hits", () => {
    expect(
      searchResultHref(
        result("chunk-1", "context_page", {
          kind: "content_chunk",
          document_id: "00000000-0000-4000-8000-000000000101",
        }), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101");
  });

  it("opens library artifacts by their own identity", () => {
    expect(
      searchResultHref(
        result(
          "artifact-1",
          "library_artifact",
          { kind: "library_artifact", id: "artifact-1" },
          "/library/item-1.md",
        ), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/library/artifacts/artifact-1");
  });

  it("opens an artifact directly without inferring its parent from a concept path", () => {
    expect(
      searchResultHref(
        result("artifact-1", "library_artifact", {
          kind: "library_artifact",
          id: "artifact-1",
        }), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/library/artifacts/artifact-1");
    expect(
      searchResultHref(result("artifact-1", "library_artifact"), "00000000-0000-4000-8000-000000000001"),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/library/artifacts/artifact-1");
  });

  it("opens graph citation versions on their parent library item", () => {
    expect(
      searchResultHref({
        id: "version-1",
        type: "library_item_version",
        concept_path: "/library/item-1.md",
        source_ref: { kind: "library_item_version", id: "version-1" },
      }, "00000000-0000-4000-8000-000000000001"),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/library/items/item-1");
  });

  it("opens context claim hits on their parent context page", () => {
    expect(
      searchResultHref(
        result("claim-1", "context_claim", {
          kind: "context_page",
          id: "00000000-0000-4000-8000-000000000101",
        }), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101");
  });

  it("opens feedback hits on their target document when provided", () => {
    expect(
      searchResultHref(
        result("feedback-1", "feedback", {
          kind: "context_page",
          id: "00000000-0000-4000-8000-000000000101",
        }), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101");
    expect(
      searchResultHref(
        result("feedback-2", "feedback", {
          kind: "library_item",
          id: "item-1",
        }), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/library/items/item-1");
    expect(
      searchResultHref(
        result("feedback-3", "feedback", {
          kind: "agent_page",
          id: "agent-1",
        }), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/agent/pages/agent-1");
    for (const [kind, id, href, destination] of [
      ["context_page", "00000000-0000-4000-8000-000000000101", "/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101", "context"],
      ["library_item", "item-1", "/workspaces/00000000-0000-4000-8000-000000000001/library/items/item-1", "library"],
      ["agent_page", "agent-1", "/workspaces/00000000-0000-4000-8000-000000000001/agent/pages/agent-1", "agent"],
    ]) {
      expect(
        searchResultLink(result("feedback-1", "feedback", { kind, id }), "00000000-0000-4000-8000-000000000001"),
      ).toEqual({ href, destination });
    }
    expect(searchResultLink(result("feedback-1", "feedback"), "00000000-0000-4000-8000-000000000001")).toBeNull();
  });

  it("opens change set hits only when a review item is known", () => {
    expect(
      searchResultHref(
        result("change-1", "change_set", {
          kind: "review_item",
          id: "review-1",
        }), "00000000-0000-4000-8000-000000000001"
      ),
    ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/review/review-1");
    expect(searchResultHref(result("change-2", "change_set"), "00000000-0000-4000-8000-000000000001")).toBeNull();
    expect(
      searchResultLink(result("change-1", "change_set", {
        kind: "review_item", id: "review-1",
      }), "00000000-0000-4000-8000-000000000001"),
    ).toEqual({ href: "/workspaces/00000000-0000-4000-8000-000000000001/review/review-1", destination: "review" });
    expect(searchResultLink(result("change-2", "change_set"), "00000000-0000-4000-8000-000000000001")).toBeNull();
  });

  it("keeps legacy source hits as library links", () => {
    expect(searchResultHref(result("source-1", "source"), "00000000-0000-4000-8000-000000000001")).toBe(
      "/workspaces/00000000-0000-4000-8000-000000000001/library/items/source-1",
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
            document_id: "00000000-0000-4000-8000-000000000101",
          }),
          "00000000-0000-4000-8000-000000000001",
        ),
      ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101");
      expect(
        searchResultLink(
          result("chunk-1", "context_page", {
            kind: "content_chunk", document_id: "00000000-0000-4000-8000-000000000101",
          }),
          "00000000-0000-4000-8000-000000000001",
        ),
      ).toEqual({
        href: "/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101",
        destination: "context",
      });
    });

    it("builds the canonical Context permalink for a context claim hit", () => {
      expect(
        searchResultHref(
          result("claim-1", "context_claim", {
            kind: "context_page",
            id: "00000000-0000-4000-8000-000000000101",
          }),
          "00000000-0000-4000-8000-000000000001",
        ),
      ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101");
      expect(
        searchResultLink(result("claim-1", "context_claim"), "00000000-0000-4000-8000-000000000001"),
      ).toBeNull();
    });

    it("builds the canonical Context permalink for a feedback hit on a context page", () => {
      expect(
        searchResultHref(
          result("feedback-1", "feedback", {
            kind: "context_page",
            id: "00000000-0000-4000-8000-000000000101",
          }),
          "00000000-0000-4000-8000-000000000001",
        ),
      ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/context/pages/00000000-0000-4000-8000-000000000101");
    });

    it("builds an explicit artifact URL for non-Context targets", () => {
      expect(
        searchResultHref(
          result(
            "artifact-1",
            "library_artifact",
            { kind: "library_artifact", id: "artifact-1" },
            "/library/item-1.md",
          ),
          "00000000-0000-4000-8000-000000000001",
        ),
      ).toBe("/workspaces/00000000-0000-4000-8000-000000000001/library/artifacts/artifact-1");
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
