import { describe, expect, it, vi } from "vitest";

import {
  DEFAULT_PAGE_METADATA,
  normalizeAliasesInput,
  normalizeTagsInput,
  parsePageDocument,
  serializePageDocument,
} from "../pageFrontmatter";

describe("page frontmatter", () => {
  it("parses editable page metadata and hides frontmatter from the body", () => {
    const parsed = parsePageDocument([
      "---",
      'title: "Release Notes"',
      "slug: release-notes",
      'icon: "lucide:rocket"',
      'cover: "https://example.com/cover.png"',
      "type: decision",
      'tags: ["Migration", "infra"]',
      'aliases: ["Release Plan", "Launch Brief"]',
      "custom: keep-me",
      "---",
      "",
      "# Release Notes",
    ].join("\n"));

    expect(parsed.body).toBe("# Release Notes");
    expect(parsed.metadata).toMatchObject({
      icon: "lucide:rocket",
      tags: ["Migration", "infra"],
      aliases: ["Release Plan", "Launch Brief"],
      type: "decision",
    });
    expect(parsed.metadata.extraLines).toEqual([
      'cover: "https://example.com/cover.png"',
      "custom: keep-me",
    ]);
  });

  it("serializes metadata as first-byte YAML frontmatter", () => {
    const markdown = serializePageDocument({
      body: "# Body",
      metadata: {
        ...DEFAULT_PAGE_METADATA,
        icon: "lucide:book-open",
        tags: ["migration", "infra"],
        aliases: ["Release Plan", "Launch Brief"],
        type: "guide",
      },
      slug: "release-notes",
      title: "Release Notes",
    });

    expect(markdown).toMatch(/^---\ntitle: "Release Notes"\nslug: release-notes\n/);
    expect(markdown).toContain('icon: "lucide:book-open"');
    expect(markdown).not.toContain("\ncover:");
    expect(markdown).toContain("type: guide");
    expect(markdown).toContain('tags: ["migration", "infra"]');
    expect(markdown).toContain('aliases: ["Release Plan", "Launch Brief"]');
    expect(markdown.endsWith("# Body")).toBe(true);
  });

  it("round-trips Unicode, quotes, commas, and backslashes in metadata strings", () => {
    const alias = '경로 "C:\\\\docs"';
    const comma = 'alpha,"beta"\\suffix';
    const markdown = serializePageDocument({
      body: "# 본문",
      metadata: {
        ...DEFAULT_PAGE_METADATA,
        aliases: [alias, comma],
        tags: ["한글태그", "경로\\이름", comma],
      },
      slug: "unicode-metadata",
      title: '한국어 "제목"',
    });

    const parsed = parsePageDocument(markdown);

    expect(parsed.metadata.tags).toEqual(["한글태그", "경로\\이름", comma]);
    expect(parsed.metadata.aliases).toEqual([alias, comma]);
  });

  it("decodes legacy JSON-escaped Unicode frontmatter scalars", () => {
    const parsed = parsePageDocument([
      "---",
      'tags: ["\\ud55c\\uae00\\ud0dc\\uadf8"]',
      'aliases: ["\\uacbd\\ub85c\\\\docs"]',
      "---",
      "",
      "# Body",
    ].join("\n"));

    expect(parsed.metadata.tags).toEqual(["한글태그"]);
    expect(parsed.metadata.aliases).toEqual(["경로\\docs"]);
  });

  it.each([
    ["remote", 'cover: "https://example.com/track.png"'],
    ["relative", "cover: /private/cover.png"],
    ["data", "cover: data:image/svg+xml;base64,PHN2Zy8+"],
    ["javascript", "cover: javascript:alert(1)"],
  ])("keeps a legacy %s cover line inert and exact", (_kind, coverLine) => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const source = [
      "---",
      "title: Legacy",
      coverLine,
      "type: overview",
      "---",
      "Body",
    ].join("\n");

    const parsed = parsePageDocument(source);
    const saved = serializePageDocument({
      body: parsed.body,
      metadata: parsed.metadata,
      slug: "legacy",
      title: "Legacy",
    });

    expect(parsed.metadata).not.toHaveProperty("cover");
    expect(parsed.metadata.extraLines).toContain(coverLine);
    expect(saved.split("\n").filter((line) => line === coverLine)).toHaveLength(1);
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("normalizes comma-separated tag input", () => {
    expect(normalizeTagsInput("Migration Plan, #Infra, migration plan, R&D")).toEqual([
      "Migration Plan",
      "Infra",
      "R&D",
    ]);
  });

  it("normalizes comma-separated alias input without stripping hash characters", () => {
    expect(normalizeAliasesInput("Launch Plan, #Hash Alias, launch plan")).toEqual([
      "Launch Plan",
      "#Hash Alias",
    ]);
  });

  it("does not silently truncate over-limit aliases or drop aliases after the limit", () => {
    const longAlias = "a".repeat(81);
    const aliases = Array.from({ length: 33 }, (_, index) => `Alias ${index + 1}`);

    expect(normalizeAliasesInput(longAlias)).toEqual([longAlias]);
    expect(normalizeAliasesInput(aliases.join(", "))).toHaveLength(33);
  });

  it("preserves markdown that starts with a thematic break instead of frontmatter", () => {
    const body = ["---", "This is just markdown.", "---", "", "# Body"].join("\n");

    const parsed = parsePageDocument(body, { tags: ["existing"] });

    expect(parsed.body).toBe(body);
    expect(parsed.metadata.tags).toEqual(["existing"]);
  });

  it("does not treat colon text between thematic breaks as page frontmatter", () => {
    const body = ["---", "Note: this is body text.", "---", "", "# Body"].join("\n");

    const parsed = parsePageDocument(body);

    expect(parsed.body).toBe(body);
  });

  it("does not strip unknown-only frontmatter-like blocks", () => {
    const body = ["---", "custom: keep-me", "---", "", "# Body"].join("\n");

    const parsed = parsePageDocument(body);

    expect(parsed.body).toBe(body);
  });

  it("keeps an explicit empty tags list instead of falling back to server tags", () => {
    const parsed = parsePageDocument([
      "---",
      "tags: []",
      "---",
      "",
      "# Body",
    ].join("\n"), { tags: ["stale"] });

    expect(parsed.body).toBe("# Body");
    expect(parsed.metadata.tags).toEqual([]);
  });

  it("keeps an explicit empty aliases list instead of falling back to server aliases", () => {
    const parsed = parsePageDocument([
      "---",
      "aliases: []",
      "---",
      "",
      "# Body",
    ].join("\n"), { aliases: ["stale"] });

    expect(parsed.body).toBe("# Body");
    expect(parsed.metadata.aliases).toEqual([]);
  });

  it("parses block-style tag lists without preserving orphan list items", () => {
    const parsed = parsePageDocument([
      "---",
      "tags:",
      "  - Migration",
      "  - Infra",
      "custom: keep-me",
      "---",
      "",
      "# Body",
    ].join("\n"));
    const serialized = serializePageDocument({
      body: parsed.body,
      metadata: parsed.metadata,
      slug: "body",
      title: "Body",
    });

    expect(parsed.metadata.tags).toEqual(["Migration", "Infra"]);
    expect(parsed.metadata.extraLines).toEqual(["custom: keep-me"]);
    expect(serialized).toContain('tags: ["Migration", "Infra"]');
    expect(serialized).not.toContain("  - Migration");
  });

  it("parses block-style aliases without preserving orphan list items", () => {
    const parsed = parsePageDocument([
      "---",
      "aliases:",
      "  - Release Plan",
      "  - Launch Brief",
      "custom: keep-me",
      "---",
      "",
      "# Body",
    ].join("\n"));
    const serialized = serializePageDocument({
      body: parsed.body,
      metadata: parsed.metadata,
      slug: "body",
      title: "Body",
    });

    expect(parsed.metadata.aliases).toEqual(["Release Plan", "Launch Brief"]);
    expect(parsed.metadata.extraLines).toEqual(["custom: keep-me"]);
    expect(serialized).toContain('aliases: ["Release Plan", "Launch Brief"]');
    expect(serialized).not.toContain("  - Release Plan");
  });

  it("does not preserve indented payloads under known scalar frontmatter keys", () => {
    const parsed = parsePageDocument([
      "---",
      "title: |",
      "  Stale Title",
      "slug: release-notes",
      "custom: keep-me",
      "---",
      "",
      "# Body",
    ].join("\n"));
    const serialized = serializePageDocument({
      body: parsed.body,
      metadata: parsed.metadata,
      slug: "release-notes",
      title: "Release Notes",
    });

    expect(parsed.metadata.extraLines).toEqual(["custom: keep-me"]);
    expect(serialized).toContain("custom: keep-me");
    expect(serialized).not.toContain("Stale Title");
  });
});
