import { describe, expect, it } from "vitest";

import { contextGraphQuery } from "../graph";

describe("contextGraphQuery", () => {
  it("requests the current two-hop Local scene with the bounded UI budget", () => {
    expect(contextGraphQuery("page-id")).toMatchObject({
      seeds: [{ kind: "context_page", id: "page-id" }],
      scope: "local",
      depth: 2,
      time: { mode: "current" },
      budget: { nodes: 250, edges: 750 },
    });
  });

  it("keeps historical modes on their supported one-hop contract", () => {
    expect(contextGraphQuery("page-id", "appearance")).toMatchObject({
      depth: 1,
      time: { mode: "appearance" },
      budget: { nodes: 50, edges: 150 },
    });
  });

  it("allows the current Local scene to request direct relationships only", () => {
    expect(contextGraphQuery("page-id", "current", 1)).toMatchObject({
      depth: 1,
      time: { mode: "current" },
      budget: { nodes: 250, edges: 750 },
    });
  });

  it.each(["appearance", "action", "as_of"] as const)(
    "never requests unsupported depth two for %s history",
    (mode) => {
      expect(contextGraphQuery("page-id", mode, 2)).toMatchObject({
        depth: 1,
        time: { mode },
        budget: { nodes: 50, edges: 150 },
      });
    },
  );
});
