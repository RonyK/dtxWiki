import { describe, expect, it } from "vitest";

import { codePointLength, inputTextError, markdownInputError, trimShortText } from "./inputPolicy";

describe("content input policy", () => {
  it("counts supplementary Unicode once and trims only the approved spaces", () => {
    expect(codePointLength("😀".repeat(256))).toBe(256);
    expect(inputTextError(` ${"😀".repeat(256)} `, 256)).toBe("");
    expect(inputTextError("😀".repeat(257), 256)).not.toBe("");
    expect(trimShortText("\ufeffAlice\ufeff")).toBe("\ufeffAlice\ufeff");
  });
  it("rejects raw controls but preserves multiline content", () => {
    expect(inputTextError("Alice\nBob", 256)).not.toBe("");
    expect(inputTextError("Alice\n\tBob", 256, false)).toBe("");
    expect(inputTextError("a\ud800", 256, false)).not.toBe("");
    expect(markdownInputError(`# ${"x".repeat(257)}`)).not.toBe("");
    expect(markdownInputError(`---\ntitle: Fine\n---\n\`\`\`bash\n# ${"x".repeat(257)}\n\`\`\`\n# Fine`)).toBe("");
  });
});
