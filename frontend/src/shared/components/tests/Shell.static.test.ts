import { describe, expect, it } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

const styles = readAppStyles();
const stylesWithoutComments = styles.replace(/\/\*[\s\S]*?\*\//g, "");

function ruleBlock(selector: string) {
  const start = styles.indexOf(`\n${selector} {`);
  expect(start, `expected a "${selector}" rule in index.css`).toBeGreaterThanOrEqual(0);
  return styles.slice(start, styles.indexOf("}", start));
}

function atRuleBlocks(atRule: string) {
  const blocks: string[] = [];
  let cursor = 0;
  while (cursor < stylesWithoutComments.length) {
    const start = stylesWithoutComments.indexOf(atRule, cursor);
    if (start < 0) {
      break;
    }
    const openingBrace = stylesWithoutComments.indexOf("{", start);
    if (openingBrace < 0) {
      throw new Error(`expected an opening brace after ${atRule}`);
    }
    let depth = 0;
    let end = -1;
    for (let index = openingBrace; index < stylesWithoutComments.length; index += 1) {
      if (stylesWithoutComments[index] === "{") {
        depth += 1;
      } else if (stylesWithoutComments[index] === "}") {
        depth -= 1;
        if (depth === 0) {
          end = index + 1;
          break;
        }
      }
    }
    if (end < 0) {
      throw new Error(`expected a closing brace after ${atRule}`);
    }
    blocks.push(stylesWithoutComments.slice(start, end));
    cursor = end;
  }
  return blocks;
}

describe("shared chrome CSS regressions", () => {
  it("keeps a tappable scrim behind the mobile nav drawer", () => {
    expect(ruleBlock(".mobile-nav-scrim")).toContain("inset: 0");
    expect(ruleBlock(".mobile-nav-drawer")).toContain("position: relative");
  });

  it("layers Shell navigation surfaces above feature controls and below shared dialogs", () => {
    const navigationLayer = Number(
      styles.match(/--z-navigation-overlay:\s*(\d+);/)?.[1],
    );
    const dialogLayer = Number(styles.match(/--z-dialog:\s*(\d+);/)?.[1]);
    const featureLocalLayers = Array.from(
      stylesWithoutComments.matchAll(/z-index:\s*(\d+);/g),
      (match) => Number(match[1]),
    );

    expect(Math.max(...featureLocalLayers)).toBeLessThan(navigationLayer);
    expect(navigationLayer).toBeLessThan(dialogLayer);
    expect(ruleBlock(".mobile-nav-backdrop")).toContain(
      "z-index: var(--z-navigation-overlay)",
    );
    expect(styles).toMatch(
      /\.command-dropdown,\s*\.create-menu-panel,\s*\.workspace-popover\s*\{[^}]*z-index: var\(--z-navigation-overlay\);/,
    );
    expect(ruleBlock(".workspace-control-error-standalone")).toContain(
      "z-index: var(--z-navigation-overlay)",
    );
  });

  it("hides the native search cancel control for SearchField inputs", () => {
    expect(styles).toContain(".search-field input::-webkit-search-decoration");
    expect(styles).toContain(".search-field input::-webkit-search-cancel-button");
  });

  it("renders status pills and metric card titles without title-casing", () => {
    expect(ruleBlock(".status-pill")).not.toContain("text-transform");
    expect(ruleBlock(".metric-card-title")).not.toContain("text-transform");
  });

  it("stacks review auto-approve policy rows on narrow screens", () => {
    expect(styles).toMatch(
      /\.review-auto-approve-row,[\s\S]{0,600}?grid-template-columns: 1fr;/,
    );
  });

  it("ellipsizes breadcrumb labels on a single line", () => {
    const label = ruleBlock(".breadcrumb-label");
    expect(label).toContain("overflow: hidden");
    expect(label).toContain("text-overflow: ellipsis");
    expect(label).toContain("white-space: nowrap");
    expect(styles).toContain(".breadcrumb-item:last-child .breadcrumb-label");
  });

  it("suppresses the skip link's shadow until the link is revealed", () => {
    expect(ruleBlock(".skip-link")).toContain("box-shadow: none");
    expect(ruleBlock(".skip-link:focus-visible")).toContain("box-shadow: var(--shadow-2)");
  });

  it("re-asserts the 44px tap target for scoped segmented controls on mobile", () => {
    // Multiple feature-specific blocks share this breakpoint. The relevant
    // contract is containment in any 1024px block, not placement after the
    // last unrelated block in the concatenated stylesheet.
    const tabletMobileBlocks = atRuleBlocks("@media (max-width: 1024px)");
    expect(
      tabletMobileBlocks.some((block) =>
        /\.review-filter-axis \.segmented-control button,\s*\.mcp-chat-filters \.segmented-control button\s*\{[^}]*min-height:\s*44px;/.test(
          block,
        ),
      ),
      "expected the scoped tap-target override inside a 1024px media query",
    ).toBe(true);
  });

  it("keeps Shell touch targets at 44px on coarse pointers without changing compact defaults", () => {
    const coarsePointerBlocks = atRuleBlocks("@media (any-pointer: coarse)");
    const shellBlock = coarsePointerBlocks.find(
      (block) =>
        block.includes(".app-shell .workspace-switcher-button") &&
        block.includes(".app-shell .nav-item") &&
        block.includes(".app-shell .command-item") &&
        block.includes(".app-shell .create-menu-item") &&
        block.includes(".app-shell .workspace-menu-item-actions > a"),
    );

    expect(
      shellBlock,
      "expected a shell-scoped coarse-pointer rule",
    ).toBeDefined();
    expect(shellBlock).toContain("min-width: 44px");
    expect(shellBlock).toContain("min-height: 44px");
    expect(ruleBlock(".workspace-switcher-button")).toContain("min-height: 32px");
    expect(ruleBlock(".command-item")).toContain("min-height: 36px");
    expect(ruleBlock(".create-menu-item")).toContain("min-height: 36px");
    expect(ruleBlock(".nav-item")).toContain("min-height: 36px");
  });

  it("lets the medium-width command search yield to header actions", () => {
    const mediumBlocks = atRuleBlocks(
      "@media (min-width: 1025px) and (max-width: 1240px)",
    );
    const shellBlock = mediumBlocks.find(
      (block) =>
        block.includes(
          "grid-template-columns: minmax(260px, max-content) minmax(120px, 1fr) auto",
        ) && block.includes(".topbar-search kbd"),
    );

    expect(shellBlock, "expected a tablet-width topbar sizing rule").toBeDefined();
    expect(shellBlock).toContain(".workspace-switcher-button");
    expect(shellBlock).toContain("min-width: 144px");
    expect(shellBlock).toContain(".topbar-actions .language-toggle span");
  });

  it("reserves FAB clearance only for screens that declare floating actions", () => {
    const tabletMobileBlocks = atRuleBlocks("@media (max-width: 1024px)");
    expect(
      tabletMobileBlocks.some((block) =>
        /\.workspace-screen\[data-mobile-action-reserve="true"\] \.feature-main\s*\{[^}]*padding-bottom:\s*calc\(180px \+ env\(safe-area-inset-bottom\)\);/.test(
          block,
        ),
      ),
      "expected opt-in FAB clearance inside a 1024px media query",
    ).toBe(true);
  });

  it("stacks account-recovery actions into mobile-width tap targets", () => {
    expect(styles).toMatch(
      /@media \(max-width: 640px\)[\s\S]*?\.admin-user-recovery-actions \{[\s\S]*?grid-template-columns: 1fr;/,
    );
    expect(styles).toMatch(
      /@media \(max-width: 640px\)[\s\S]*?\.admin-user-recovery-action \{[\s\S]*?width: 100%;/,
    );
  });
});
