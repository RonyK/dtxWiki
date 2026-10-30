import { describe, expect, it } from "vitest";

import { workspaceLinkDestinationId } from "../workspaceLinkBadge";

describe("workspace link badge destinations", () => {
  it("recognizes only explicit product routes to a different workspace", () => {
    const source = "11111111-1111-4111-8111-111111111111";
    const foreign = "22222222-2222-4222-8222-222222222222";
    const page = "33333333-3333-4333-8333-333333333333";
    const origin = "https://wiki.example";
    const cases: Array<[string, string | null]> = [
      [`/p/${foreign}/${page}`, foreign],
      [`/p/${source}/${page}`, null],
      [`/p/${foreign}/${page}?tab=history#chapter`, foreign],
      [`https://wiki.example/workspaces/${foreign}/agent/pages/${page}/edit`, foreign],
      [`https://elsewhere.example/workspaces/${foreign}/context`, null],
      [`//wiki.example/workspaces/${foreign}/context`, null],
      [`/workspaces/${foreign}/context/unknown/${page}`, null],
      [`/workspaces/${foreign}/context/pages/not-a-uuid`, null],
      [`/api/v1/workspaces/${foreign}/context/pages/${page}`, null],
      [`/p/${foreign}/${page}?target=${page}`, null],
    ];

    for (const [href, expected] of cases) {
      expect(workspaceLinkDestinationId(href, source, origin), href).toBe(expected);
    }
    expect(workspaceLinkDestinationId(`/p/${foreign}/${page}`, null, origin)).toBeNull();
  });
});
