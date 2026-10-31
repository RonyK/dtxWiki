import { describe, expect, it, vi } from "vitest";

import type { GraphRenderEdge } from "../../shared/rendererTypes";
import { localizeWorkspaceRenderEdges } from "../localizeRenderEdges";

const edge = (id: string, kind: string): GraphRenderEdge => ({
  id, source: "original-source", target: "original-target", edgeKind: kind,
  label: "raw", layer: "knowledge", count: 7, direction: "directed",
});

describe("workspace render edge labels", () => {
  it("translates each unique kind once rather than once per original relationship", () => {
    const input = Array.from({ length: 10_000 }, (_, index) =>
      edge(String(index), index % 2 ? "wikilink" : "citation_page"));
    const describeKind = vi.fn((kind: string) => `localized:${kind}`);
    const result = localizeWorkspaceRenderEdges(input, describeKind);
    expect(describeKind.mock.calls).toEqual([["citation_page"], ["wikilink"]]);
    expect(result).toHaveLength(input.length);
    result.forEach((value, index) => {
      expect(value).toEqual({ ...input[index], label: `localized:${input[index].edgeKind}` });
      expect(value).not.toBe(input[index]);
    });
    expect(input.every((value) => value.label === "raw")).toBe(true);
  });

  it("does not share labels across languages, calls, scenes or viewers", () => {
    const input = [edge("first", "wikilink"), edge("second", "wikilink")];
    const english = vi.fn(() => "Link"), korean = vi.fn(() => "링크");
    expect(localizeWorkspaceRenderEdges(input, english).map((value) => value.label)).toEqual(["Link", "Link"]);
    expect(localizeWorkspaceRenderEdges(input, korean).map((value) => value.label)).toEqual(["링크", "링크"]);
    expect(english).toHaveBeenCalledTimes(1);
    expect(korean).toHaveBeenCalledTimes(1);
    const nextViewer = vi.fn(() => "Next viewer");
    expect(localizeWorkspaceRenderEdges([edge("other-scene", "wikilink")], nextViewer)[0].label).toBe("Next viewer");
    expect(nextViewer).toHaveBeenCalledTimes(1);
  });

  it("preserves fallback keys, unknown and empty labels without inferring relations", () => {
    const input = [
      { ...edge("fallback", "unused"), edgeKind: undefined, label: "custom" },
      { ...edge("unknown", "custom"), edge_kind: "different-legacy-field" },
      edge("empty", ""), edge("empty-again", ""),
    ];
    const describeKind = vi.fn((kind: string) => kind === "" ? "" : `label:${kind}`);
    expect(localizeWorkspaceRenderEdges(input, describeKind).map((value) => value.label))
      .toEqual(["label:custom", "label:custom", "", ""]);
    expect(describeKind.mock.calls).toEqual([["custom"], [""]]);
    expect(localizeWorkspaceRenderEdges([], describeKind)).toEqual([]);
    expect(describeKind).toHaveBeenCalledTimes(2);
  });
});
