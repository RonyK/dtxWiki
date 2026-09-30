import { describe, expect, it } from "vitest";

import { domainLabel, humanizeEnum } from "../domainLabel";

describe("humanizeEnum", () => {
  it("sentence-cases a snake_case enum", () => {
    expect(humanizeEnum("manual_upload")).toBe("Manual upload");
    expect(humanizeEnum("extracted_text")).toBe("Extracted text");
    expect(humanizeEnum("not_promoted")).toBe("Not promoted");
  });

  it("preserves known acronyms in any position", () => {
    expect(humanizeEnum("oauth_scope")).toBe("OAuth scope");
    expect(humanizeEnum("mcp_client")).toBe("MCP client");
    expect(humanizeEnum("source_url")).toBe("Source URL");
    expect(humanizeEnum("api")).toBe("API");
    expect(humanizeEnum("json_export")).toBe("JSON export");
  });

  it("handles already-spaced input and empty strings", () => {
    expect(humanizeEnum("changes requested")).toBe("Changes requested");
    expect(humanizeEnum("")).toBe("");
    expect(humanizeEnum("__")).toBe("__");
  });
});

describe("domainLabel", () => {
  it("returns the locale-map value when the key exists", () => {
    // agent.json defines labels.category.system_prompt = "System prompt".
    expect(domainLabel("agent", "category", "system_prompt")).toBe(
      "System prompt",
    );
  });

  it("falls back to an acronym-aware humanized form for unmapped values", () => {
    expect(domainLabel("library", "connectorType", "manual_upload")).toBe(
      "Manual upload",
    );
    expect(domainLabel("settings", "policyKey", "oauth_scope")).toBe(
      "OAuth scope",
    );
  });

  it("returns the unset label for null/undefined/empty values", () => {
    expect(domainLabel("agent", "category", null)).toBe("unset");
    expect(domainLabel("agent", "category", undefined)).toBe("unset");
    expect(domainLabel("agent", "category", "")).toBe("unset");
  });

  it("uses a provided TFunction when passed", () => {
    const t = ((key: string, options?: { defaultValue?: string }) =>
      key === "labels.category.system_prompt"
        ? "Custom prompt"
        : (options?.defaultValue ?? key)) as unknown as Parameters<
      typeof domainLabel
    >[3];
    expect(domainLabel("agent", "category", "system_prompt", t)).toBe(
      "Custom prompt",
    );
  });
});
