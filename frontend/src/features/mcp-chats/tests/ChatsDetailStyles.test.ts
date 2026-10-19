import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const chatStyles = readFileSync(
  resolve(process.cwd(), "src/styles/06-chats-review-queue.css"),
  "utf8",
);
const lintStyles = readFileSync(
  resolve(process.cwd(), "src/styles/09-lint-findings-fixes.css"),
  "utf8",
);

function ruleBodyIn(sheet: string, selector: string, startAt = 0) {
  const ruleStart = sheet.indexOf(`${selector} {`, startAt);
  if (ruleStart < 0) {
    throw new Error(`Missing CSS rule: ${selector}`);
  }
  const bodyStart = sheet.indexOf("{", ruleStart) + 1;
  const bodyEnd = sheet.indexOf("}", bodyStart);
  return sheet.slice(bodyStart, bodyEnd);
}

function ruleBody(selector: string, startAt = 0) {
  return ruleBodyIn(chatStyles, selector, startAt);
}

describe("MCP chat detail sizing styles", () => {
  it("lets every nested flow grid shrink to the 390px content band", () => {
    for (const selector of [
      ".mcp-chat-flow-layout",
      ".mcp-chat-flow-panel",
      ".mcp-chat-flow-call",
      ".mcp-chat-flow-call-failure",
      ".mcp-chat-edit-card",
      ".mcp-chat-edit-preview",
      ".mcp-tool-tab-panel",
      ".mcp-tool-panel-card",
      ".mcp-tool-result-meta",
    ]) {
      const rule = ruleBody(selector);
      expect(rule, selector).toContain("min-width: 0;");
      expect(rule, selector).toContain("max-width: 100%;");
    }
  });

  it("keeps desktop auto-fit columns but collapses tool metadata to one mobile column", () => {
    expect(ruleBody(".mcp-tool-result-meta")).toContain(
      "grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));",
    );

    const mobileStart = chatStyles.indexOf("@media (max-width: 640px)");
    expect(mobileStart).toBeGreaterThanOrEqual(0);
    expect(ruleBody(".mcp-tool-result-meta", mobileStart)).toContain(
      "grid-template-columns: minmax(0, 1fr);",
    );
  });

  it("wraps long metadata and confines preformatted values to an internal scroller", () => {
    expect(ruleBody(".mcp-tool-result-meta dd")).toContain(
      "overflow-wrap: anywhere;",
    );

    const pre = ruleBody(".mcp-tool-tab-panel pre");
    expect(pre).toContain("max-width: 100%;");
    expect(pre).toContain("overflow: auto;");
    expect(pre).toContain("overflow-wrap: anywhere;");

    const longValue = ruleBody(".mcp-tool-longvalue-body");
    expect(longValue).toContain("max-width: 100%;");
    expect(longValue).toContain("overflow: auto;");
    expect(longValue).toContain("overflow-wrap: anywhere;");
  });

  it("lets max-length client identity shrink without hiding metadata overflow", () => {
    for (const selector of [
      ".mcp-chat-meta-strip",
      ".mcp-chat-meta-item",
      ".mcp-client-identity",
      ".mcp-client-identity-detail",
    ]) {
      const rule = ruleBody(selector);
      expect(rule, selector).toContain("min-width: 0;");
      expect(rule, selector).toContain("max-width: 100%;");
      expect(rule, selector).not.toContain("overflow: hidden;");
    }

    expect(ruleBody(".mcp-chat-meta-value")).toContain(
      "overflow-wrap: anywhere;",
    );
    const preview = ruleBody(".mcp-client-identity-preview");
    expect(preview).toContain("-webkit-line-clamp: 1;");
    expect(preview).toContain("overflow-wrap: anywhere;");

    const mobileStart = chatStyles.indexOf("@media (max-width: 640px)");
    expect(ruleBody(".mcp-chat-meta-strip", mobileStart)).toContain(
      "grid-template-columns: minmax(0, 1fr);",
    );
  });

  it("keeps client sizing independent from the four-column facts group", () => {
    const clientItem = ruleBody(".mcp-chat-meta-item--client");
    expect(ruleBody(".mcp-chat-meta-strip")).toContain("grid-template-columns: minmax(0, 4fr) minmax(0, 3fr);");
    expect(ruleBody(".mcp-chat-session-facts")).toContain("repeat(4, minmax(0, 1fr))");
    expect(clientItem).toContain("min-width: 0;");
    expect(ruleBody(".mcp-client-identity > summary")).toContain("min-height: 44px;");
    expect(ruleBody(".mcp-client-identity-detail")).toContain(
      "grid-template-columns: repeat(2, minmax(0, 1fr));",
    );

    const mobileStart = chatStyles.indexOf("@media (max-width: 640px)");
    expect(ruleBody(".mcp-chat-meta-item--client", mobileStart)).toContain(
      "grid-column: 1 / -1;",
    );
    expect(ruleBody(".mcp-chat-meta-item--client", mobileStart)).toContain(
      "min-width: 0;",
    );
    expect(ruleBody(".mcp-client-identity-detail", mobileStart)).toContain(
      "grid-template-columns: minmax(0, 1fr);",
    );
  });

  it("keeps client identifiers wrappable and copy controls compact", () => {
    expect(ruleBody(".mcp-client-identifier > code")).toContain("min-width: 0;");
    expect(ruleBody(".mcp-client-identity-detail li > code,\n.mcp-client-identity-detail li > span"))
      .toContain("overflow-wrap: anywhere;");

    expect(chatStyles).not.toContain(".mcp-client-identifier > .compact-icon-button");

  });

  it("renders the transcript caveat as a persistent two-line banner body", () => {
    const notice = ruleBody(".mcp-chat-record-notice");
    expect(notice).toContain("display: grid;");
    expect(notice).toContain("gap: 3px;");
    expect(notice).toContain("max-width: 100%;");
  });

  it("pins the flow rail and panel to the viewport and scrolls each independently", () => {
    const layout = ruleBody(".mcp-chat-flow-layout");
    expect(layout).toContain("position: sticky;");
    // Not height: 100% -- .mcp-chat-flow-section (its containing block) has
    // no definite height of its own, so a percentage here would silently
    // fall back to content-height and never actually track the viewport
    // (confirmed live: identical computed height at 900px and 500px window
    // heights until this was changed to a viewport-relative calc).
    expect(layout).toContain("height: calc(100vh - var(--topbar-h));");

    expect(ruleBody(".mcp-chat-flow-rail-body")).toContain("overflow-y: auto;");
    expect(ruleBody(".mcp-chat-flow-panel")).toContain("overflow-y: auto;");
  });

  it("confines a call's ReaderTabs row to its own horizontal scroller", () => {
    // Regression: this rule was dropped once already when the old
    // .mcp-timeline-body .reader-tabs-row selector it replaced was deleted
    // along with the rest of the pre-flow accordion CSS, and narrow
    // viewports (<=360px) genuinely overflowed the call card as a result.
    // It moved again when the tabs came out of the "technical details"
    // disclosure and onto the call card itself.
    const row = ruleBody(".mcp-chat-flow-call .reader-tabs-row");
    expect(row).toContain("min-width: 0;");
    expect(row).toContain("max-width: 100%;");
    expect(row).toContain("overflow-x: auto;");
    // Always visible on every call of a step, so no full-width reader
    // header's 16px trailing margin.
    expect(row).toContain("margin: 10px 0 0;");

    const mobileStart = chatStyles.indexOf("@media (max-width: 640px)");
    expect(ruleBody(".mcp-chat-flow-call .reader-tabs", mobileStart)).toContain(
      "min-width: max-content;",
    );
  });

  it("leaves the tab height to the shared touch floor", () => {
    // 07-settings-policy.css raises .reader-tab to 44px under
    // (max-width: 1024px), (any-pointer: coarse). A scoped (0,2,0) height
    // here would silently beat that (0,1,0) floor.
    expect(chatStyles).not.toMatch(/\.mcp-chat-flow-call[^{]*\.reader-tab\b[^{]*\{[^}]*min-height/);
  });

  it("wraps edit previews and keeps a long one scrolling inside the card", () => {
    const text = ruleBody(".mcp-chat-edit-text");
    expect(text).toContain("min-width: 0;");
    expect(text).toContain("white-space: pre-wrap;");
    expect(text).toContain("overflow-wrap: anywhere;");
    expect(text).toContain("max-height: 10.5em;");
    expect(text).toContain("overflow-y: auto;");
    // The before/after marker gutter is fixed; the text track shrinks.
    expect(ruleBody(".mcp-chat-edit-line")).toContain(
      "grid-template-columns: 20px minmax(0, 1fr);",
    );
    expect(ruleBody(".mcp-chat-edit-summary")).toContain("overflow-wrap: anywhere;");
    expect(ruleBody(".mcp-chat-flow-call-message")).toContain("white-space: pre-wrap;");
  });

  it("drops the retired call disclosures along with their rules", () => {
    for (const selector of [
      ".mcp-chat-flow-call-tech",
      ".mcp-tool-technical-details",
      ".mcp-chat-call-meta",
    ]) {
      expect(chatStyles, selector).not.toContain(selector);
    }
    const technical = ruleBody(".mcp-tool-technical-inputs");
    expect(technical).toContain("min-width: 0;");
    expect(technical).toContain("max-width: 100%;");
  });

  it("drops the pinned two-pane layout for a single-column accordion at 1024px", () => {
    const narrowStart = chatStyles.indexOf("@media (max-width: 1024px) {");
    expect(narrowStart).toBeGreaterThanOrEqual(0);
    const layout = ruleBody(".mcp-chat-flow-layout", narrowStart);
    expect(layout).toContain("position: static;");
    expect(ruleBody(".mcp-chat-flow-rail-body", narrowStart)).toContain(
      "overflow: visible;",
    );
  });

  it("gives a context link the lint queue's bordered, icon-led chip proportions", () => {
    // Mirrors 09-lint-findings-fixes.css .lint-target-chip: bordered box,
    // icon + truncating label, context-tinted, same touch-target floor.
    const chip = ruleBody(".mcp-chat-dl-link");
    const reference = ruleBodyIn(lintStyles, ".lint-target-chip");
    for (const declaration of [
      "display: inline-flex;",
      "align-items: center;",
      "min-height: 30px;",
      "color: var(--context);",
    ]) {
      expect(reference, "reference .lint-target-chip").toContain(declaration);
      expect(chip, ".mcp-chat-dl-link").toContain(declaration);
    }
    expect(chip).toContain("border: 1px solid var(--border);");
    expect(chip).toContain("border-radius: 7px;");
    expect(chip).toContain("text-decoration: none;");

    const label = ruleBody(".mcp-chat-dl-link > span");
    expect(label).toContain("overflow: hidden;");
    expect(label).toContain("text-overflow: ellipsis;");
    expect(label).toContain("white-space: nowrap;");

    const touchStart = chatStyles.indexOf(
      "@media (max-width: 1024px), (any-pointer: coarse) {",
    );
    expect(touchStart).toBeGreaterThanOrEqual(0);
    expect(ruleBody(".mcp-chat-dl-link", touchStart)).toContain(
      "min-height: 44px;",
    );
  });
});
