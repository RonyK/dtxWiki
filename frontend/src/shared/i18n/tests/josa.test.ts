import { describe, expect, it } from "vitest";

import { i18n } from "../index";
import { attachJosa } from "../josa";

describe("attachJosa", () => {
  it("picks the batchim form after a Hangul syllable with a final consonant", () => {
    expect(attachJosa("문서", "josaIGa")).toBe("문서가"); // "서" has no batchim -> 가
    expect(attachJosa("초안", "josaEulReul")).toBe("초안을"); // "안" has batchim -> 을
  });

  it("picks the no-batchim form after a Hangul syllable without a final consonant", () => {
    expect(attachJosa("메모", "josaEunNeun")).toBe("메모는");
    expect(attachJosa("초안", "josaEuRo")).toBe("초안으로");
  });

  it("falls back to the parenthesized pair for a numeric tail", () => {
    expect(attachJosa("v12", "josaEulReul")).toBe("v12을(를)");
  });

  it("falls back to the parenthesized pair for a Latin tail", () => {
    expect(attachJosa("Untitled", "josaIGa")).toBe("Untitled이(가)");
  });

  it("falls back to the parenthesized pair for a UUID tail", () => {
    expect(attachJosa("a1b2c3d4-0000-0000-0000-000000000000", "josaEunNeun")).toBe(
      "a1b2c3d4-0000-0000-0000-000000000000은(는)",
    );
  });

  it("returns the value unchanged for an unknown format specifier", () => {
    expect(attachJosa("문서", "unknownFmt")).toBe("문서");
  });

  it("renders only the particle for the *Only variant, e.g. after a closing quote", () => {
    expect(attachJosa("초안", "josaEuRoOnly")).toBe("으로");
    expect(attachJosa("메모", "josaEuRoOnly")).toBe("로");
  });

  it("falls back to the bracketed pair for the *Only variant on a non-Hangul tail", () => {
    expect(attachJosa("v12", "josaEulReulOnly")).toBe("을(를)");
  });

  it("returns empty string for the *Only variant with an unknown pair", () => {
    expect(attachJosa("문서", "unknownFmtOnly")).toBe("");
  });
});

// I18N-06 / I18N-05: this is the wiring, not just the pure function.
// `i18n.services.formatter.add(...)` is the real registration mechanism —
// i18next v21+ dropped the `interpolation.format` callback (it now
// unconditionally rebinds that option to `services.formatter.format`
// during init, so passing our own function there is silently ignored). A
// formatter that isn't actually registered degrades ungracefully: i18next
// logs a warning and renders the interpolated value with no particle at
// all, which a unit test of `attachJosa` alone would never catch.
describe("josa i18next Formatter wiring", () => {
  it("resolves a real t() call through the registered josaEulReul format", () => {
    // "3" is a digit, not a Hangul syllable, so this exercises the
    // bracketed-fallback branch end to end.
    expect(i18n.t("agent:history.compareTitle", { version: "3", lng: "ko" })).toBe(
      "Agent v3을(를) 현재와 비교",
    );
    // A Hangul-ending value must resolve to the bare particle, proving the
    // Formatter is genuinely dispatching through attachJosa and not just
    // echoing the raw value.
    expect(i18n.t("agent:history.compareTitle", { version: "하나", lng: "ko" })).toBe(
      "Agent v하나를 현재와 비교",
    );
  });

  it("resolves the *Only variant through a real t() call", () => {
    expect(
      i18n.t("settings:mcp.tokenRenamed", {
        requested: "old-name",
        saved: "새이름",
        lng: "ko",
      }),
    ).toBe('"old-name" 이름이 이미 사용 중이어서 이 키를 "새이름"으로 저장했습니다.');
  });
});
