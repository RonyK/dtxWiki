import { describe, expect, it } from "vitest";

import { i18n } from "../../i18n";
import { formatVersionRange } from "../versionRange";

describe("formatVersionRange", () => {
  it("compresses only consecutive runs of three or more versions", () => {
    const t = i18n.getFixedT("en", "review");
    expect(formatVersionRange([5, 4], t)).toBe("v4, v5");
    expect(formatVersionRange([7, 4, 6, 5], t)).toBe("v4–v7 (4 versions)");
    // A gap is never bridged: v5 is not part of this list.
    expect(formatVersionRange([4, 6, 7], t)).toBe("v4, v6, v7 (3 versions)");
    expect(formatVersionRange([4, 5, 6, 8, 9, 10, 11], t)).toBe("v4–v6, v8–v11 (7 versions)");
  });
});
