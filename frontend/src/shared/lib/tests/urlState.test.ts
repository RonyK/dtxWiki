import { describe, expect, it } from "vitest";

import {
  applyPeriodParam,
  parsePeriodParam,
  serializePeriodParam,
} from "../urlState";

const PERIODS = ["today", "week", "month", "all"] as const;

describe("urlState", () => {
  it("parses period params with default fallback", () => {
    expect(parsePeriodParam(null, PERIODS, "today")).toBe("today");
    expect(parsePeriodParam("week", PERIODS, "today")).toBe("week");
    expect(parsePeriodParam("bogus", PERIODS, "today")).toBe("today");
  });

  it("serializes default period as an omitted param", () => {
    expect(serializePeriodParam("today", "today")).toBeNull();
    expect(serializePeriodParam("week", "today")).toBe("week");
  });

  it("deletes the key when a period value equals the default", () => {
    const params = new URLSearchParams("period=week&other=x");
    const next = applyPeriodParam(params, "period", "today", "today");

    expect(next.get("period")).toBeNull();
    expect(next.get("other")).toBe("x");
  });

  it("sets the key when a period value differs from the default", () => {
    const params = new URLSearchParams("");
    const next = applyPeriodParam(params, "period", "week", "today");

    expect(next.get("period")).toBe("week");
  });

  it("does not mutate input params when applying a period value", () => {
    const params = new URLSearchParams("period=week");

    applyPeriodParam(params, "period", "today", "today");

    expect(params.get("period")).toBe("week");
  });
});
