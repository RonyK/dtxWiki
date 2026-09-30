import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const styles = readFileSync(
  resolve(process.cwd(), "src/styles/11-chats-filter-fixes.css"),
  "utf8",
);

describe("Chats list mobile filter styles", () => {
  it("collapses only at the 640px phone breakpoint", () => {
    expect(styles).toContain("@media (max-width: 640px)");
    expect(styles).toMatch(
      /\.mcp-chat-filter-disclosure-toggle\s*\{[^}]*display:\s*inline-flex;/s,
    );
    expect(styles).toMatch(
      /\.mcp-chat-filter-disclosure-panel\[hidden\]\s*\{[^}]*display:\s*none\s*!important;/s,
    );
  });

  it("keeps every tablet and mobile filter target at least 44px tall", () => {
    expect(styles).toContain("@media (max-width: 1024px)");
    expect(styles).toMatch(
      /\.mcp-chat-filter-disclosure\s+:is\([\s\S]*?\.search-field,[\s\S]*?\.select-field select,[\s\S]*?\.segmented-control button,[\s\S]*?\.filter-chip,[\s\S]*?\.mcp-chat-filters-reset[\s\S]*?\)\s*\{[^}]*min-height:\s*44px;/,
    );
    expect(styles).toMatch(
      /\.mcp-chat-filter-disclosure-toggle\s*\{[^}]*min-height:\s*44px;/s,
    );
  });
});
