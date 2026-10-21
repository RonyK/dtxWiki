import { afterEach, describe, expect, it, vi } from "vitest";
import { writeTextToClipboard } from "../clipboard";

const originalExec = Object.getOwnPropertyDescriptor(document, "execCommand");
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalSecure = Object.getOwnPropertyDescriptor(window, "isSecureContext");

afterEach(() => {
  for (const [object, key, descriptor] of [
    [document, "execCommand", originalExec],
    [navigator, "clipboard", originalClipboard],
    [window, "isSecureContext", originalSecure],
  ] as const) {
    if (descriptor) Object.defineProperty(object, key, descriptor);
    else Reflect.deleteProperty(object, key);
  }
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("clipboard browser fallbacks", () => {
  it("copies the complete value on HTTP, removes the carrier and restores focus", async () => {
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();
    const copy = vi.fn(() => {
      const carrier = document.querySelector("textarea")!;
      expect(carrier.value).toBe("full-key-and-command\nsecond-line");
      expect(carrier.selectionEnd).toBe(carrier.value.length);
      return true;
    });
    Object.defineProperty(document, "execCommand", { configurable: true, value: copy });
    await expect(writeTextToClipboard("full-key-and-command\nsecond-line")).resolves.toBe(true);
    expect(copy).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it.each([false, "throw"])("does not report success when legacy copy returns %s", async (failure) => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(document, "execCommand", { configurable: true, value: () => { if (failure === "throw") throw new Error("denied"); return false; } });
    await expect(writeTextToClipboard("dummy-key")).resolves.toBe(false);
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("uses legacy copy after modern API permission denial", async () => {
    const modern = vi.fn().mockRejectedValue(new Error("denied"));
    const legacy = vi.fn(() => true);
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: modern } });
    Object.defineProperty(document, "execCommand", { configurable: true, value: legacy });
    await expect(writeTextToClipboard("dummy-key")).resolves.toBe(true);
    expect(modern).toHaveBeenCalledWith("dummy-key");
    expect(legacy).toHaveBeenCalledWith("copy");
  });
});
