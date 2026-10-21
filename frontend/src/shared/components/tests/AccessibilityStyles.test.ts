import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

function styles(file: string) {
  return readFileSync(resolve(process.cwd(), `src/styles/${file}`), "utf8");
}

function atRuleBlocks(source: string, atRule: string) {
  const blocks: string[] = [];
  const withoutComments = source.replace(/\/\*[\s\S]*?\*\//g, "");
  let cursor = 0;
  while (cursor < withoutComments.length) {
    const start = withoutComments.indexOf(atRule, cursor);
    if (start < 0) {
      break;
    }
    const openingBrace = withoutComments.indexOf("{", start);
    let depth = 0;
    let end = -1;
    for (let index = openingBrace; index < withoutComments.length; index += 1) {
      if (withoutComments[index] === "{") {
        depth += 1;
      } else if (withoutComments[index] === "}") {
        depth -= 1;
        if (depth === 0) {
          end = index + 1;
          break;
        }
      }
    }
    if (openingBrace < 0 || end < 0) {
      throw new Error(`Unclosed CSS at-rule: ${atRule}`);
    }
    blocks.push(withoutComments.slice(start, end));
    cursor = end;
  }
  return blocks;
}

describe("cross-route accessibility styles", () => {
  it("uses the audited contrast-safe soft metadata color", () => {
    expect(styles("01-base.css")).toContain("--ink-soft: #56667b;");
  });

  it("uses accessible shared tree geometry tokens and keeps Review selectors at least 24px", () => {
    const knowledge = styles("04-knowledge.css");
    const activity = styles("05-review-activity-search.css");
    const queue = styles("06-chats-review-queue.css");
    expect(knowledge).toContain("--tree-row-block-size: 36px;");
    expect(knowledge).toContain("--tree-control-hit-size: 28px;");
    expect(knowledge).toMatch(
      /\.tree-row-chevron\s*\{[^}]*width:\s*var\(--tree-control-hit-size\);[^}]*height:\s*var\(--tree-control-hit-size\);/s,
    );
    expect(activity).toMatch(
      /\.review-bulk-select-all\s*\{[^}]*min-width:\s*24px;[^}]*min-height:\s*24px;/s,
    );
    expect(queue).toMatch(
      /\.review-row-select\s*\{[^}]*width:\s*24px;[^}]*height:\s*24px;/s,
    );
  });

  it("forces regular weight on every select/textarea/input/button (L-13)", () => {
    // Settings' Goal textarea and Default language select rendered visibly
    // bolder than the sibling Workspace title input, even though `font:
    // inherit` (a shorthand covering font-weight) already applied to all
    // four -- an explicit longhand is what actually wins.
    expect(styles("01-base.css")).toMatch(
      /button,\s*input,\s*select,\s*textarea\s*\{[^}]*font:\s*inherit;[^}]*font-weight:\s*400;/s,
    );
  });

  it("keeps the mobile sidebar/topbar-search hide in the shell stylesheet, not Settings (M-3)", () => {
    const shell = styles("02-shell.css");
    const settings = styles("07-settings-policy.css");
    expect(shell).toMatch(
      /@media \(max-width: 1024px\)\s*\{\s*\.topbar-search,\s*\.sidebar\s*\{[^}]*display:\s*none;/,
    );
    expect(settings).not.toMatch(/\.topbar-search,\s*\.sidebar\s*\{/);
  });

  it("sizes the editor tag chip box to its content instead of a fixed 320px (L-3)", () => {
    const editor = styles("03-editor.css");
    expect(editor).toMatch(
      /\.editor-title-tags:has\(\.chip-input-chip\)\s*\{[^}]*flex:\s*0 1 auto;[^}]*max-width:\s*320px;/s,
    );
    expect(editor).not.toMatch(
      /\.editor-title-tags:has\(\.chip-input-chip\)\s*\{[^}]*flex:\s*0 1 320px;/s,
    );
  });

  it("desaturates a disabled primary icon button instead of just fading it (L-1)", () => {
    // Save draft / Publish / Create draft / Send all render as
    // .icon-button.primary; opacity:0.5 alone left them a lighter purple
    // that still read as clickable.
    expect(styles("03-editor.css")).toMatch(
      /\.icon-button\.primary:disabled\s*\{[^}]*background:\s*var\(--surface-active\);[^}]*color:\s*var\(--ink-faint\);/s,
    );
  });

  it("overrides CodeMirror's low-contrast default placeholder color (A-2)", () => {
    // CodeMirror's own baseTheme ships `.cm-placeholder { color: #888 }`,
    // which measured 3.54:1 on the editor's white background -- short of
    // WCAG AA's 4.5:1 for text. --ink-muted (#475569) measures ~7.6:1.
    expect(styles("03-editor.css")).toMatch(
      /\.cm-placeholder\s*\{[^}]*color:\s*var\(--ink-muted\);/s,
    );
  });

  it("uses hover affordances only for fine pointers and 44px tree targets on narrow or touch layouts", () => {
    const knowledge = styles("04-knowledge.css");
    expect(knowledge).toMatch(
      /@media \(hover: hover\) and \(pointer: fine\)\s*\{[\s\S]*?\.tree-row:hover \.tree-row-actions\s*\{[^}]*opacity:\s*1;/,
    );
    expect(knowledge).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)\s*\{[\s\S]*?--tree-row-block-size:\s*48px;[\s\S]*?--tree-control-hit-size:\s*44px;/,
    );
    expect(knowledge).toMatch(
      /@container \(max-width: 360px\)\s*\{[\s\S]*?\.tree-row-add\s*\{[^}]*display:\s*none;/,
    );
    expect(knowledge).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)\s*\{[\s\S]*?\.tree-list\s*\{[^}]*margin-inline-end:\s*4px;/,
    );
  });

  it("draws non-interactive tree depth guides that never take a row state colour", () => {
    const knowledge = styles("04-knowledge.css");
    expect(knowledge).toMatch(
      /\.tree-row-indent-guides\s*\{[^}]*position:\s*absolute;[^}]*pointer-events:\s*none;/s,
    );
    expect(knowledge).toMatch(
      /\.tree-row-indent-guide\s*\{[^}]*width:\s*1px;[^}]*background:\s*color-mix/s,
    );
    // User-directed 2026-09-13: selecting a row must not light its ancestor
    // chain. The guides had one accent override, keyed off a hover/focus path
    // attribute -- and selecting a row focuses it, so the whole chain stayed
    // accented. Nothing may repaint a guide from row state again: the guide's
    // background is declared exactly once, in the base rule above.
    expect(knowledge).not.toContain("[data-tree-guide-path");
    const guideBackgrounds = knowledge.match(
      /\.tree-row-indent-guide\b[^{]*\{[^}]*background:/gs,
    );
    expect(guideBackgrounds).toHaveLength(1);
  });

  it("centres each tree depth guide on its ancestor's chevron", () => {
    // User-directed 2026-09-10 and previously lost to a silent revert, so the
    // contract is pinned here as well as measured at runtime in
    // tests/e2e/overall_ui_responsive.spec.ts.
    //
    // Guide N stands for the level-N ancestor and must land on that row's
    // chevron centre: the row's 2px leading pad, plus half the leading visual
    // slot the chevron is centred in, minus half the guide's own 1px width, and
    // then one --tree-indent-step per level (which .tree-row-indent-guide
    // already adds). The slot term MUST stay a var(): it is 24px at rest and
    // 32px under the touch/narrow query, and a literal drifts on one of them.
    const knowledge = styles("04-knowledge.css");
    const railTokens = knowledge.match(/\.tree-rail\s*\{([\s\S]*?)\}/)?.[1] ?? "";
    expect(railTokens).toMatch(
      /--tree-indent-guide-offset:\s*calc\(\s*2px \+ var\(--tree-leading-visual-slot\) \/ 2 - 0\.5px\s*\)/,
    );
    expect(railTokens).toContain("--tree-leading-visual-slot: 24px;");
    // The guide crosses the row's own chevron slot, so it has to paint above
    // .tree-row-chevron:hover's background or it renders as two stubs.
    const guidesRule =
      knowledge.match(/\.tree-row-indent-guides\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(guidesRule).toContain("z-index: 2;");
    expect(guidesRule).toContain("pointer-events: none;");
    expect(knowledge).toMatch(
      /\.tree-row > :not\(\.tree-row-indent-guides\)\s*\{[^}]*z-index:\s*1;/s,
    );
  });

  it("keeps one depth level wide enough to separate the guides, wider on touch", () => {
    // The step is also the gap between two adjacent depth guides. It started at
    // 7px on both breakpoints, which is exactly half the 14px chevron glyph, so
    // the deepest guide landed on the glyph's edge and the gutter read as one
    // bundle rather than one line per level. Alignment was never the problem --
    // measured at 390px, 768px and 1440px the guides sit on their ancestors'
    // chevron centres either way -- clearance was.
    //
    // 2026-09-13, user-directed: 12px desktop, 16px touch.
    const knowledge = styles("04-knowledge.css");
    expect(knowledge).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)[\s\S]*?--tree-indent-step:\s*16px;/,
    );
    const railTokens = knowledge.match(/\.tree-rail\s*\{([\s\S]*?)\}/)?.[1] ?? "";
    expect(railTokens).toContain("--tree-indent-step: 12px;");
    // The drag insert indicator repeats .tree-row's indent arithmetic. It held a
    // literal 7px, which stopped matching the row the moment the step moved.
    expect(knowledge).toMatch(
      /\.tree-row\.tree-row-insert-before::before[\s\S]*?left:\s*calc\(2px \+ \(var\(--tree-level\) - 1\) \* var\(--tree-indent-step\)\)/,
    );
  });

  it("keeps long tree action menus scrollable within short viewports", () => {
    const knowledge = styles("04-knowledge.css");
    const menuRule = knowledge.match(/\.tree-node-menu\s*\{([^}]*)\}/s)?.[1] ?? "";
    expect(menuRule).toContain("max-width: calc(100vw - 16px);");
    expect(menuRule).toContain("env(safe-area-inset-top)");
    expect(menuRule).toContain("env(safe-area-inset-bottom)");
    expect(menuRule).toContain("overflow-y: auto;");
  });

  it("allows an explicit small secondary icon button without the mobile size floor", () => {
    expect(styles("01-base.css")).toContain("--control-size-small: 28px;");
    const editor = styles("03-editor.css");
    const rule = editor.match(/\.icon-button\.icon-button--small\s*\{([^}]+)\}/)?.[1];
    for (const property of ["width", "height", "min-width", "min-height"]) {
      expect(rule).toContain(`${property}: var(--control-size-small);`);
    }
    expect(styles("07-settings-policy.css")).toContain(
      ".icon-button:not(.icon-button--small),",
    );
  });

  it("restores a 44px floor for compact mobile and tablet controls", () => {
    const policy = styles("07-settings-policy.css");
    const tabletMobileBlocks = atRuleBlocks(
      policy,
      "@media (max-width: 1024px)",
    );
    expect(
      tabletMobileBlocks.some((block) =>
        /\.workspace-switcher-button,\s*\.reader-tab,\s*\.review-rail \.reader-tab,\s*\.review-filter-toggle,\s*\.history-compare-toolbar \.text-button,\s*\.history-card-actions \.compact-button,\s*\.mcp-timeline-summary,\s*\.mcp-strip-toggle,\s*\.mcp-client-identity > summary,\s*\.mcp-tool-display-items a,\s*\.mcp-provenance-link,\s*\.editor-mode-tabs\.editor-mode-tabs button,\s*\.tree-filter,\s*\.tree-rail-search,\s*\.advanced-disclosure-toggle,\s*\.context-list-filters select\s*\{[^}]*min-height:\s*44px;/.test(
          block,
        ),
      ),
      "expected compact controls to have a 44px floor inside the tablet media query",
    ).toBe(true);
  });

  it("gives the reader version chip a 44px target without inflating its pill", () => {
    // This chip sits in `.reader-meta-row` beside plain 22px `.reader-chip`
    // spans, so it is deliberately absent from the min-height floor above: a
    // 44px box there made it twice its neighbours' height. The target is met by
    // an overlay instead, and both halves are pinned because dropping either
    // one silently regresses -- the pill's height, or the tap area.
    const policy = styles("07-settings-policy.css");
    const tabletMobileBlocks = atRuleBlocks(
      policy,
      "@media (max-width: 1024px)",
    );

    const floorBlock = tabletMobileBlocks.find((block) =>
      /min-height:\s*44px;/.test(block),
    );
    expect(floorBlock, "expected a 44px floor block").toBeDefined();
    expect(
      /\.reader-chip-action[,\s]*\{[^}]*min-height:\s*44px;/.test(
        floorBlock ?? "",
      ),
      "the version chip must not be raised to 44px; it has to match the plain chips beside it",
    ).toBe(false);

    expect(
      tabletMobileBlocks.some((block) =>
        /\.reader-chip-action\s*\{[^}]*position:\s*relative;/.test(block),
      ),
      "expected .reader-chip-action to anchor its hit-area overlay",
    ).toBe(true);
    expect(
      tabletMobileBlocks.some((block) =>
        /\.reader-chip-action::after\s*\{[^}]*inset-block:\s*-11px;/.test(block),
      ),
      "expected a transparent ::after extending the 22px pill to a 44px touch target",
    ).toBe(true);
    expect(
      tabletMobileBlocks.some((block) =>
        /\.reader-tab,\s*\.mcp-strip-toggle,\s*\.mcp-tool-display-items a,\s*\.mcp-provenance-link\s*\{[^}]*min-width:\s*44px;/.test(
          block,
        ),
      ),
      "expected narrow tabs and document links to have a 44px width floor",
    ).toBe(true);
  });

  it("gives every bare <select> a themed chevron instead of the raw OS dropdown", () => {
    // M-1 (260908 UI audit): a <select> with no styled ancestor wrapper
    // (Search scope, Agent create dialog, Settings language, ...) rendered as
    // the unstyled native dropdown next to custom-styled siblings. A global
    // element rule supplies the border/radius/chevron; more specific rules
    // still win on specificity for their own sizing.
    const base = styles("01-base.css");
    const selectRule = /(^|\n)select\s*\{([^}]*)\}/.exec(base);
    expect(selectRule, "expected a bare `select` rule in 01-base.css").toBeTruthy();
    const body = selectRule![2];
    expect(body).toMatch(/appearance:\s*none/);
    expect(body).toContain("background-image: url(");
    expect(body).toContain("border: 1px solid var(--border)");
  });

  it("never lets a scoped select/input rule's background shorthand erase the base chevron", () => {
    // The `background` shorthand resets background-image to `none` even when
    // only background-color was intended, silently deleting the chevron from
    // 01-base.css's `select` rule for every more-specific selector below.
    // Longhand background-color is required wherever a rule's selector
    // targets (or can match) a <select> element.
    for (const file of [
      "01-base.css",
      "03-editor.css",
      "04-knowledge.css",
      "05-review-activity-search.css",
      "06-chats-review-queue.css",
      "07-settings-policy.css",
      "08-responsive-deployment.css",
      "09-lint-findings-fixes.css",
      "10-library-policy-fixes.css",
      "11-chats-filter-fixes.css",
    ]) {
      const source = styles(file).replace(/\/\*[\s\S]*?\*\//g, "");
      for (const match of source.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
        const [, selector, decl] = match;
        // The `.select-control` carve-out went with the rule it excused: a
        // complete, unused dropdown skin whose `background` shorthand was the
        // only reason this check needed an exception.
        const targetsSelect = /(^|[\s,>~+(])select(?=[\s,)[:{]|$)/.test(selector);
        if (!targetsSelect) continue;
        expect(
          decl,
          `${file}: "${selector.trim().slice(0, 80)}" must use background-color, ` +
            "not the background shorthand (it resets the chevron to none)",
        ).not.toMatch(/(^|;)\s*background\s*:/);
      }
    }
  });
});
