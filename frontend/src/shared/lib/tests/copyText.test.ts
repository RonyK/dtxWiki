import { afterEach, describe, expect, it, vi } from "vitest";

import { copyTextWithReason } from "../copyText";

describe("copyTextWithReason", () => {
  afterEach(() => vi.restoreAllMocks());

  it("reports a successful secure clipboard write", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    await expect(copyTextWithReason("https://example.test/p/1")).resolves.toEqual({
      status: "copied",
    });
  });

  it("preserves permission denial while selecting the manual fallback", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    const target = document.createElement("code");
    target.textContent = "/p/1";
    document.body.append(target);

    await expect(copyTextWithReason("/p/1", target)).resolves.toEqual({
      status: "selected",
      reason: "permission",
    });
    target.remove();
  });

  it("preserves the automatic-copy cause when no manual target exists", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    await expect(copyTextWithReason("/p/1", null)).resolves.toEqual({
      status: "unavailable",
      reason: "unsupported",
    });
  });

  it("reports a failed visible selection without exposing an exception", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    const target = document.createElement("code");
    const getSelection = vi.spyOn(window, "getSelection").mockReturnValue(null);

    await expect(copyTextWithReason("/p/1", target)).resolves.toEqual({
      status: "unavailable",
      reason: "selection",
    });
    getSelection.mockRestore();
  });
});
