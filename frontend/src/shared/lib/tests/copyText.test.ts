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

  it("prepares the visible fallback only when automatic copy fails", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const prepare = vi.fn().mockReturnValue(null);
    await copyTextWithReason("full-id", prepare);
    expect(prepare).not.toHaveBeenCalled();
    writeText.mockRejectedValueOnce(new Error("denied"));
    await copyTextWithReason("full-id", prepare);
    expect(prepare).toHaveBeenCalledOnce();
  });

  it("leaves the current selection alone when the old target has detached", async () => {
    let reject!: (error: Error) => void;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: () => new Promise<void>((_, fail) => { reject = fail; }) },
    });
    const old = document.createElement("code");
    old.textContent = "old-id";
    document.body.append(old);
    const pending = copyTextWithReason("old-id", old);
    old.remove();
    const current = document.createElement("p");
    current.textContent = "New selection";
    document.body.append(current);
    const range = document.createRange();
    range.selectNodeContents(current);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    reject(new Error("denied"));
    await expect(pending).resolves.toEqual({ status: "unavailable", reason: "selection" });
    expect(window.getSelection()?.toString()).toBe("New selection");
    current.remove();
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
