import { describe, expect, it } from "vitest";

import { normalizeAccountEmail, normalizeAccountName, validateAccountPassword } from "./identityPolicy";

describe("account input units and normalization", () => {
  it("counts code points and preserves exact passwords", () => {
    expect(normalizeAccountName(" " + "😀".repeat(32) + "\u3000")).toBe("😀".repeat(32));
    expect(() => normalizeAccountName("😀".repeat(33))).toThrow();
    expect(() => validateAccountPassword("😀".repeat(128), true)).not.toThrow();
    expect(() => validateAccountPassword("😀".repeat(129))).toThrow();
    expect(() => validateAccountPassword("x".repeat(14), true)).toThrow();
    expect(() => validateAccountPassword("x".repeat(15), true)).not.toThrow();
    expect(() => validateAccountPassword(" ".repeat(15), true)).not.toThrow();
  });

  it("uses UTF-8 bytes after lowercasing for every email input", () => {
    expect(normalizeAccountEmail(" 한글@EXAMPLE.COM ")).toBe("한글@example.com");
    expect(normalizeAccountEmail("a@" + "b".repeat(252))).toHaveLength(254);
    expect(() => normalizeAccountEmail("a@" + "b".repeat(253))).toThrow();
    expect(() => normalizeAccountEmail("한".repeat(21) + "aa@b")).toThrow();
    expect(() => normalizeAccountEmail("İ".repeat(22) + "@b")).toThrow();
  });

  it.each(["\n", "\u0085", "\u2028", "\ud800"])("rejects raw invalid name/email characters before trimming", (character) => {
    expect(() => normalizeAccountName(character)).toThrow();
    expect(() => normalizeAccountEmail(character + "a@b")).toThrow();
  });
});
