import type { CompletionContext } from "@codemirror/autocomplete";
import { describe, expect, it } from "vitest";

import { createEditorCompletionSource, isLikelyUuid } from "../editorCompletions";

describe("editor completions", () => {
  it("suggests canonical wikilinks from loaded pages and keeps slugs searchable", () => {
    const source = createEditorCompletionSource(
      {
        current: [
          {
            id: "p1",
            slug: "release-notes",
            title: "Roadmap",
            page_type: "overview",
            lifecycle_status: "active",
            aliases: [],
            current_version_id: "v1",
            updated_at: "2026-05-20T00:00:00Z",
            is_public: false,
          },
        ],
      },
      { current: [] },
    );

    const result = source(completionContext("[[release"));

    expect(result?.from).toBe(2);
    expect(result?.options).toContainEqual(
      expect.objectContaining({
        apply: "release-notes|Roadmap]]",
        displayLabel: "Roadmap",
        detail: "release-notes",
        label: "Roadmap release-notes",
      }),
    );
  });

  it("suggests readable citation references and inserts source ids", () => {
    const source = createEditorCompletionSource(
      { current: [] },
      {
        current: [
          sourceSummary("src-1", "API Design Source", "https://example.com/api"),
          sourceSummary("src-2", "API Design Source", "https://example.com/other"),
        ],
      },
    );

    const result = source(completionContext("@cite{api design"));

    expect(result?.from).toBe("@cite{".length);
    expect(result?.options).toContainEqual(
      expect.objectContaining({
        apply: "src-1}",
        displayLabel: "API Design Source",
        detail: "src-1",
        label: "API Design Source src-1 https://example.com/api",
      }),
    );
    expect(result?.options).toContainEqual(
      expect.objectContaining({
        apply: "src-2}",
        displayLabel: "API Design Source",
        label: "API Design Source src-2 https://example.com/other",
      }),
    );
  });

  it("isLikelyUuid detects standard UUID v4 shape", () => {
    expect(isLikelyUuid("550e8400-e29b-41d4-a716-446655440000")).toBe(true);
    expect(isLikelyUuid("src-design-notes")).toBe(false);
    expect(isLikelyUuid("a1b2c3")).toBe(false);
    expect(isLikelyUuid("")).toBe(false);
  });

  it("never emits UUID-shaped source ids from the @cite picker (proposal C8)", () => {
    const source = createEditorCompletionSource(
      { current: [] },
      {
        current: [
          sourceSummary(
            "550e8400-e29b-41d4-a716-446655440000",
            "Has UUID id",
            "https://example.com/u",
          ),
          sourceSummary("src-design", "Has kebab id", "https://example.com/k"),
        ],
      },
    );

    const result = source(completionContext("@cite{"));

    const ids = (result?.options ?? []).map((opt) => opt.apply);
    expect(ids).toContain("src-design}");
    expect(ids).toContain("has-uuid-id-1tvxtq}");
    expect(ids.some((id) => typeof id === "string" && id.includes("550e8400"))).toBe(
      false,
    );
  });

  it("derives stable citation keys for current REST source UUIDs", () => {
    const source = createEditorCompletionSource(
      { current: [] },
      {
        current: [
          sourceSummary(
            "550e8400-e29b-41d4-a716-446655440000",
            "API Design Source",
            "https://example.com/api",
          ),
        ],
      },
    );

    const first = source(completionContext("@cite{api"));
    const second = source(completionContext("@cite{api"));

    expect(first?.options[0]).toMatchObject({
      apply: "api-design-source-8hqvik}",
      detail: "api-design-source-8hqvik",
      displayLabel: "API Design Source",
      label: "API Design Source api-design-source-8hqvik https://example.com/api",
    });
    expect(second?.options[0]?.apply).toBe(first?.options[0]?.apply);
  });
});

function completionContext(text: string) {
  return {
    explicit: false,
    matchBefore(pattern: RegExp) {
      const match = pattern.exec(text);
      if (!match) {
        return null;
      }
      return {
        from: text.length - match[0].length,
        text: match[0],
        to: text.length,
      };
    },
  } as CompletionContext;
}

function sourceSummary(id: string, title: string, uri: string) {
  return {
    id,
    uri,
    title,
    connector_type: "manual",
    format_hint: "markdown",
    trust_level: "trusted",
    lifecycle_status: "active",
    current_version_id: "v1",
    updated_at: "2026-05-20T00:00:00Z",
  };
}
