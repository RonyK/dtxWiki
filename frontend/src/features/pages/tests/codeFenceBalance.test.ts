import { describe, expect, it } from "vitest";

import { appendClosingCodeFence, findUnclosedCodeFence } from "../codeFenceBalance";

describe("codeFenceBalance", () => {
  it("reports the editor-relative line and closing marker for an unclosed fence", () => {
    expect(findUnclosedCodeFence("# Heading\n\n````ts\nconst value = 1;\n")).toEqual({
      closingFence: "````",
      line: 3,
    });
  });

  it("requires a same-marker closing fence at least as long as the opener", () => {
    expect(findUnclosedCodeFence("````\n```\ninside\n````\n")).toBeNull();
    expect(findUnclosedCodeFence("~~~js\n```\n~~~\n")).toBeNull();
  });

  it("appends the author's matching closing fence at the end of the buffer", () => {
    expect(appendClosingCodeFence("~~~js\nvalue", "~~~")).toBe("~~~js\nvalue\n~~~\n");
  });
});
