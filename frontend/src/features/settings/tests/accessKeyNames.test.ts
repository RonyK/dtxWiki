import { describe, expect, it } from "vitest";

import { defaultTokenNameForApp, uniqueTokenName } from "../SettingsScreen";

describe("generated access key names", () => {
  it("fits an automatic name derived from a 160-code-point app label", () => {
    const label = "😀".repeat(160);
    const name = defaultTokenNameForApp(label);
    expect(Array.from(name)).toHaveLength(128);
    expect(name).toMatch(/ access key$/);
  });

  it("reserves suffix space in code points and preserves surrogate pairs", () => {
    const explicit = "😀".repeat(128);
    expect(uniqueTokenName(explicit, [])).toBe(explicit);
    const next = uniqueTokenName(explicit, [explicit]);
    expect(next).toBe("😀".repeat(126) + " 2");
    expect(Array.from(next)).toHaveLength(128);
  });
});
