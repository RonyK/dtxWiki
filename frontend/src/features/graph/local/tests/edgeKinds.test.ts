import { describe, expect, it } from "vitest";

import { GRAPH_EDGE_PATTERN } from "../../shared/graphTokens";
import { LOCAL_EDGE_VISUAL_KIND_ORDER, localEdgeVisualKind } from "../edgeKinds";

describe("Local edge visual kinds", () => {
  it.each([
    ["wikilink", "link"],
    ["citation_page", "citation"],
    ["citation_source", "citation"],
    ["ask_citation", "citation"],
    ["co_access", "coAccess"],
    ["tree", "membership"],
    ["searched", "action"],
    ["returned", "action"],
    ["retrieved", "action"],
    ["included", "action"],
    ["cited", "action"],
    ["created", "action"],
    ["edited", "action"],
    ["reviewed", "action"],
    ["approved", "action"],
    ["rejected", "action"],
    ["request_changes", "action"],
    ["published", "action"],
    ["restored", "action"],
    ["superseded", "action"],
    ["merged", "action"],
    ["promoted", "action"],
    ["tool_produced", "action"],
    ["referenced", "action"],
  ] as const)("maps %s to %s", (kind, visualKind) => {
    expect(localEdgeVisualKind(kind)).toBe(visualKind);
  });

  it("keeps stored relationships and actions in an explicit, stable Local drawing order", () => {
    expect(LOCAL_EDGE_VISUAL_KIND_ORDER).toEqual([
      "link",
      "citation",
      "coAccess",
      "membership",
      "action",
    ]);
    expect(LOCAL_EDGE_VISUAL_KIND_ORDER).toEqual(Object.keys(GRAPH_EDGE_PATTERN));
  });

  it.each([
    undefined,
    null,
    "",
    "evidence",
    "citation",
    "promoted_from",
    "page_provenance",
    "unknown_future_citation",
    "unknown_future_action",
  ])("omits unknown or non-Local kinds (%s), without layer inference", (kind) => {
    expect(localEdgeVisualKind(kind)).toBeNull();
  });
});
