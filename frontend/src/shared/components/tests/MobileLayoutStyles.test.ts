import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(
  resolve(process.cwd(), "src/styles/08-responsive-deployment.css"),
  "utf8",
);
const workspaceStyles = readFileSync(
  resolve(process.cwd(), "src/styles/07-settings-policy.css"),
  "utf8",
);
const activityStyles = readFileSync(
  resolve(process.cwd(), "src/styles/05-review-activity-search.css"),
  "utf8",
);
const baseStyles = readFileSync(
  resolve(process.cwd(), "src/styles/01-base.css"),
  "utf8",
);
const shellStyles = readFileSync(
  resolve(process.cwd(), "src/styles/02-shell.css"),
  "utf8",
);
const knowledgeStyles = readFileSync(
  resolve(process.cwd(), "src/styles/04-knowledge.css"),
  "utf8",
);

describe("mobile layout regression styles", () => {
  it("keeps Activity filter controls inside the phone content column", () => {
    // The four selectors share this declaration block; the final selector lets
    // the rule helper capture that combined block without coupling to spacing.
    expect(ruleBody(".activity-filter-group")).toContain("max-width: 100%");
    expect(ruleBody(".activity-filters")).toContain("display: grid");
    expect(ruleBody(".activity-filters")).toContain(
      "grid-template-columns: minmax(0, 1fr)",
    );
    expect(ruleBody(".activity-subtype-row .segmented-control"))
      .toContain("flex-wrap: wrap");
  });

  it("gives Account Workspace identity and actions separate mobile rows", () => {
    expect(ruleBody(".workspace-access-row")).toContain("flex-wrap: wrap");
    expect(ruleBody(".workspace-access-row > div:first-of-type"))
      .toContain("flex: 1 1 calc(100% - 28px)");
    expect(ruleBody(".workspace-access-row-actions"))
      .toContain("flex: 1 1 100%");
  });

  it("pins the workspace switcher inside both phone viewport edges", () => {
    const popover = ruleBody(".workspace-popover");
    expect(popover).toContain("position: fixed");
    expect(popover).toContain("left: 12px");
    expect(popover).toContain("right: 12px");
    expect(popover).toContain("width: auto");
    expect(popover).toContain(
      "max-height: calc(100dvh - var(--topbar-h) - 12px)",
    );
  });

  it("lets the phone workspace switcher use the available brand width", () => {
    const controls = ruleBody(".workspace-controls", workspaceStyles);
    expect(controls).toContain("flex: 1 1 auto");
    expect(controls).toContain("min-width: 0");
    // Matched inside its own media block rather than through ruleBody, which
    // returns the LAST rule for a selector -- the phone block below also
    // styles .workspace-switcher-button, and silently shadowed this assertion.
    expect(workspaceStyles).toMatch(
      /@media \(max-width: 1024px\)[\s\S]*?\s\.workspace-switcher-button \{[^}]*width:\s*100%;/,
    );
    expect(workspaceStyles).toMatch(
      /@media \(max-width: 480px\)\s*\{[\s\S]*?\.topbar-brand \{[^}]*padding-inline:\s*8px;[\s\S]*?\.topbar-brand \.prism-glyph\s*\{[^}]*display:\s*none;/,
    );
  });

  it("gives the tablet workspace switcher the width its brand cell has", () => {
    // The ceiling used to be a flat min(210px, 100%) that never lifted, so at
    // 768x1024 the switcher sat at 210px inside a 576px brand cell, and at
    // 1024x768 inside an 832px one -- measured in the running app.
    expect(workspaceStyles).toMatch(
      /@media \(max-width: 1024px\)[\s\S]*?\s\.workspace-switcher-button \{[^}]*max-width:\s*min\(340px, 100%\);/,
    );
    // The label carried a second, unconditional 200px cap, so raising the
    // button alone changed nothing visible.
    expect(ruleBody(".workspace-switcher-label", shellStyles)).not.toContain(
      "max-width:",
    );
    // A <button> centres its text; harmless at 184px, stranding at 340px.
    expect(ruleBody(".workspace-switcher-label", shellStyles)).toContain(
      "text-align: start",
    );
  });

  it("drops the redundant initials badge on a phone so the name gets its track", () => {
    // At 390px the brand cell is 204px and the nav toggle takes 44 of it. The
    // badge abbreviates a name that is displayed right next to it, so it is
    // the first thing that should yield its 22px track.
    expect(workspaceStyles).toMatch(
      /@media \(max-width: 480px\)[\s\S]*?\.workspace-initials \{[^}]*display:\s*none;/,
    );
    expect(workspaceStyles).toMatch(
      /@media \(max-width: 480px\)[\s\S]*?\.workspace-switcher-button,[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) 14px;/,
    );
  });

  it("docks Context mobile actions in a header row instead of over content", () => {
    expect(ruleBody(".workspace-screen", workspaceStyles)).toContain(
      "grid-template-rows: auto minmax(0, 1fr)",
    );
    expect(
      ruleBody(
        ".workspace-screen > .workspace-mobile-drawer-toggle",
        workspaceStyles,
      ),
    ).toContain("align-self: start");
    expect(ruleBody(".workspace-screen > .feature-main", workspaceStyles))
      .toContain("grid-row: 2");

    expect(ruleBody(".context-screen", workspaceStyles)).toContain(
      "grid-template-rows: auto minmax(0, 1fr)",
    );

    const actions = ruleBody(
      ".context-screen .workspace-mobile-actions",
      workspaceStyles,
    );
    expect(actions).toContain("position: static");
    expect(actions).toContain("grid-row: 1");
    expect(actions).toContain("flex-direction: row");

    const main = ruleBody(".context-screen > .feature-main", workspaceStyles);
    expect(main).toContain("grid-row: 2");
  });

  it("raises compact Markdown and editor popover controls for narrow and coarse pointers", () => {
    for (const selector of [
      ".markdown-code-copy",
      ".mermaid-zoom-toolbar-button",
      ".mermaid-zoom-readout",
      ".editor-link-header button",
      ".editor-link-actions button",
      ".editor-comment-header button",
      ".editor-comment-actions button",
    ]) {
      expect(workspaceStyles).toContain(selector);
    }
    const controls = ruleBody(".editor-comment-actions button", workspaceStyles);
    expect(controls).toContain("min-width: 44px");
    expect(controls).toContain("min-height: 44px");
    expect(workspaceStyles).toMatch(
      /@media \(any-pointer: coarse\) \{[\s\S]*?\.markdown-code-copy,[\s\S]*?min-width: 44px;[\s\S]*?min-height: 44px;/,
    );
  });

  it("raises secondary mobile and coarse-pointer text controls to 44px", () => {
    expect(ruleBody(".activity-filter-reset", activityStyles)).toContain(
      "min-height: 44px",
    );
    expect(activityStyles.lastIndexOf(".activity-filter-reset {")).toBeGreaterThan(
      activityStyles.indexOf(".activity-filter-reset {"),
    );
    expect(workspaceStyles).toContain(
      '.settings-content input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="file"])',
    );
    expect(workspaceStyles).toContain(".settings-content select");
    expect(workspaceStyles).toContain(".ask-composer input");
    expect(workspaceStyles).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\) \{[\s\S]*?\.settings-content select,[\s\S]*?\.ask-composer input,[\s\S]*?\.empty-suggestions button,[\s\S]*?\.search-panel select,[\s\S]*?\.inline-action,[\s\S]*?\.ask-search-path summary[\s\S]*?min-width: 44px;[\s\S]*?min-height: 44px;/,
    );
    expect(workspaceStyles).toMatch(
      /\.ask-search-path summary \{\s*padding-block: 13px;/,
    );
  });

  it("keeps the page editor rail off-canvas and reserves half a phone viewport for editing", () => {
    expect(
      ruleBody(".page-workspace:not(.focus-mode)", workspaceStyles),
    ).toContain("grid-template-rows: auto minmax(0, 1fr)");
    expect(
      ruleBody(
        ".page-workspace:not(.focus-mode) > .editor-panel",
        workspaceStyles,
      ),
    ).toContain("grid-row: 2");
    expect(workspaceStyles).toMatch(
      /\.workspace-left-panel\s*\{[^}]*transform:\s*translateX\(-100%\)/,
    );

    const phoneEditor = ruleBody(".page-workspace .editor-grid");
    expect(phoneEditor).toContain("min-height: 50dvh");
    expect(phoneEditor).toContain("flex: 0 0 auto");
  });

  it("hides the topbar's middle slot below 1024px, for both auth states", () => {
    // The two-track mobile rule only works because the middle of the topbar's
    // three children is hidden at this breakpoint. Shell renders
    // `.topbar-search` there in BOTH auth states now -- the real search box
    // signed in, an aria-hidden placeholder signed out -- so this one rule
    // covers both. Shell.test.tsx pins the markup side; neither half is
    // sufficient alone, which is the lesson from this bug recurring.
    expect(shellStyles).toMatch(
      /@media \(max-width: 1024px\)\s*\{[^}]*\.topbar-search,\s*\.sidebar\s*\{[^}]*display:\s*none;/,
    );
  });

  it("stacks a wrapped tablist as one control, not two", () => {
    // `.reader-tabs` sets a single `gap: 24px`. That is column-only while the
    // list is nowrap, but the mobile block turns wrapping on -- which promoted
    // the same 24px to a row gap and split five tabs into two bands 68px
    // apart, the active underline stranded above the gap.
    expect(ruleBody(".settings-policy-tabs .reader-tabs")).toContain(
      "row-gap: 0",
    );
  });

  it("gives a breadcrumb crumb a 44px touch target on a phone", () => {
    // Measured at 43x18 on a 390px viewport: a link less than half the minimum
    // every neighbouring control honours.
    expect(ruleBody(".breadcrumb-item a")).toContain("min-height: 44px");
  });

  it("keeps the shell focusable on the viewport fallback and clips it with dvh", () => {
    // `overflow: hidden` still makes an element a scroll container -- it can
    // carry a scrollLeft with no visible scrollbar. A browser preserves that
    // offset across a viewport-size change by CLAMPING it to
    // `scrollWidth - clientWidth` instead of zeroing it, so any overflow here
    // turned into a permanent left shift of the ENTIRE app shell the moment a
    // Galaxy Fold folded down (690px unfolded -> 344px folded; 260911 Fold
    // report). `overflow: clip` cannot be a scroll container at all -- verified
    // empirically: setting `scrollLeft` on a `clip` element is silently
    // refused (stays 0) while `scrollWidth` still reports overflow correctly,
    // so this closes the bug outright rather than mitigating it, and needs
    // no change to
    // `expectNoHorizontalOverflow` (tests/e2e/overall_ui_responsive.spec.ts),
    // which reads `scrollWidth` on these same two selectors.
    // Chrome/Edge 90-107 know `clip` but not dynamic viewport units. Leaving
    // them on the 100vh fallback WITH `clip` would make the bottom band
    // focusable but invisible, because `clip` prevents programmatic reveal.
    // Keep their base rule `hidden`; Shell's scroll guard covers its residual
    // horizontal offset. Only the dynamic-viewport rule takes `clip`.
    const [shellFallback, shellDynamicViewport] = ruleBodies(
      ".app-shell",
      baseStyles,
    );
    expect(shellFallback).toContain("height: 100vh");
    expect(shellFallback).toContain("overflow: hidden");
    expect(shellFallback).not.toContain("height: 100dvh");
    expect(shellFallback).not.toContain("overflow: clip");
    expect(shellDynamicViewport).toContain("height: 100dvh");
    expect(shellDynamicViewport).toContain("overflow: clip");
    expect(baseStyles).toMatch(
      /@supports\s*\(height:\s*100dvh\)\s*\{\s*\.app-shell\s*\{[^}]*height:\s*100dvh;[^}]*overflow:\s*clip;/,
    );

    // `.main-surface` is not a viewport-height fallback, so it stays clipped
    // on every engine that supports it.
    const lastOverflow = (body: string) =>
      [...body.matchAll(/overflow:\s*([a-z]+);/g)].at(-1)?.[1];
    const mainSurface = ruleBody(".main-surface", shellStyles);
    expect(mainSurface).toMatch(/overflow:\s*hidden;/);
    expect(lastOverflow(mainSurface)).toBe("clip");
  });

  it("pins .visually-hidden to 1px inside .feature-main, beating the content-band width rules", () => {
    // `.feature-main > :not(.upload-drop-overlay)` (and the --reader/--form/
    // --wide variants beside it) share (0,2,0) specificity with
    // `.visually-hidden`'s own `width: 1px` -- and (0,2,0) beat it, so a
    // direct-child sr-only element (LintScreen's aria-live status row) had its
    // width set to `min(100%, 1200px)` instead. It painted nothing, but its
    // LAYOUT box now sat past .feature-main's own padding-box edge --
    // measured as exactly 24px of real overflow on /lint, which is the 24px
    // that got stuck as scrollLeft after a fold (260911 Fold report).
    expect(ruleBody(".feature-main > .visually-hidden", knowledgeStyles)).toContain(
      "width: 1px !important",
    );
  });

  it("collapses the signed-out topbar to the mobile two-column layout", () => {
    // `.app-shell-public .topbar` in 01-base.css sits outside every media query
    // at specificity (0,2,0), so the (0,1,0) mobile `.topbar` rule could not
    // reach it: signed-out screens kept the desktop three-column topbar on a
    // phone and its minmax(180px, …) lead track floored the shell at 343px.
    // The override has to be re-stated at matching-or-higher specificity inside
    // the mobile block, or the whole shell is clipped again below 343px.
    expect(baseStyles).toContain(".app-shell-public .topbar");
    expect(ruleBody(".app-shell-public .topbar", workspaceStyles)).toContain(
      "grid-template-columns: minmax(0, 1fr) auto",
    );
  });

  it("keeps dtxWiki visible in the compact public authentication header", () => {
    expect(workspaceStyles).toMatch(
      /@media \(max-width: 1024px\)\s*\{[\s\S]*?\.brand-name\s*\{[^}]*display:\s*none;[\s\S]*?\.app-shell-public \.brand-name\s*\{[^}]*display:\s*inline;/,
    );
  });

  it("pins every mobile shell track to minmax(0, 1fr), never a bare 1fr", () => {
    // A bare `1fr` is minmax(auto, 1fr): its floor is the item's min-content.
    // `.topbar` carries no min-width:0/overflow firewall and `.topbar-actions`
    // floors itself at max-content, so a bare 1fr let intrinsic width push the
    // shell past the viewport, where overflow:hidden clipped it silently.
    // The mobile override must cover the higher-specificity collapsed shell
    // too; reading its last selector captures the shared declaration block.
    expect(workspaceStyles).toMatch(/\.app-shell,\s*\.app-shell\[data-sidebar-collapsed="true"\]\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/);
    expect(ruleBody('.app-shell[data-sidebar-collapsed="true"]', workspaceStyles)).toContain(
      "grid-template-columns: minmax(0, 1fr)",
    );
    const mobileStart = workspaceStyles.indexOf("@media (max-width: 1024px)");
    const mobileBlock = workspaceStyles.slice(
      mobileStart,
      workspaceStyles.indexOf("@media (max-width: 640px)", mobileStart),
    );
    expect(mobileBlock).toMatch(/\.topbar\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto;/);
    expect(
      mobileBlock,
    ).toContain(
      "grid-template-columns: minmax(0, 1fr) auto",
    );
    expect(
      ruleBody('.app-shell[data-sidebar-collapsed="true"] .topbar', workspaceStyles),
    ).toContain("grid-template-columns: minmax(0, 1fr) auto");
    expect(ruleBody(".editor-shortcut-grid", workspaceStyles)).toContain(
      "grid-template-columns: minmax(0, 1fr)",
    );
    // The mobile block must not reintroduce the bare form anywhere.
    expect(
      /grid-template-columns:\s*1fr\b/.test(mobileBlock.slice(0, 6000)),
      "mobile shell block still declares a bare `1fr` track",
    ).toBe(false);
  });

  it("gives the account-notice surface an explicit column track", () => {
    // Declaring rows but no columns leaves the implicit column at `auto`
    // (min-content..max-content), so one wide child stretches the notice and
    // the entire route screen past the viewport.
    const notice = ruleBody(".main-surface--account-notice", shellStyles);
    expect(notice).toContain("display: grid");
    expect(notice).toContain("grid-template-columns: minmax(0, 1fr)");
    expect(notice).toContain("grid-template-rows: auto minmax(0, 1fr)");
  });

  it("gives the narrow-screen Settings picker a 44px trigger", () => {
    // Replaces the edge-mask test, which guarded a gradient that faded BOTH
    // edges unconditionally -- it claimed there was more content to the left
    // at scrollLeft: 0, so it passed while describing something untrue. The
    // strip it hinted at is gone; what matters now is that the one control
    // replacing it is thumb-sized.
    const trigger = ruleBody(".settings-section-picker-trigger", workspaceStyles);
    expect(trigger).toContain("min-height: 44px;");
    expect(trigger).toContain("width: 100%;");
  });

  it("lets the Ask session rail shrink instead of overflowing into the main column", () => {
    // H-2 (260908 UI audit): a grid's implicit track (and a flex item's
    // cross-axis box) defaults to fitting its widest child's content --
    // the longest session title pushed the rail 63px past
    // .ask-session-rail-body's clientWidth into the main content, clipping
    // the title mid-word instead of eliding it via .ask-session-title's
    // text-overflow:ellipsis. min-width:0 at every level of the chain (rail,
    // list grid, button grid item, header flex row) is required for the
    // ellipsis to ever take effect. Matches .tree-rail's own min-width:0.
    expect(ruleBody(".ask-session-rail", knowledgeStyles)).toContain("min-width: 0");
    expect(ruleBody(".ask-session-list", knowledgeStyles)).toContain("min-width: 0");
    expect(ruleBody(".ask-session-list button", knowledgeStyles)).toContain("min-width: 0");
    expect(ruleBody(".ask-session-header", knowledgeStyles)).toContain("min-width: 0");
  });

  it("lets the Ask screen's own composer-clearance rule outrank the shared mobile-action reservation", () => {
    // H-3 (260908 UI audit): .workspace-screen[data-mobile-action-reserve="true"]
    // .feature-main (07-settings-policy.css) reserves 180px at (0,3,0)
    // specificity for screens with a floating inspector/FAB. A plain
    // `.ask-screen .feature-main` rule is only (0,2,0), so it silently lost to
    // that shared rule no matter what padding it set -- and the extra padding
    // it WAS trying to add made things worse anyway: the sticky composer's
    // stuck position moves up by exactly its container's padding-bottom (it
    // sticks to the padding edge, not the border edge), so "reserving space"
    // pushed the composer up just as much and never closed the gap against
    // the last turn card. Measured directly: 0px gave the composer a clean
    // 18px clearance; every value from 48px to 180px converged on the same
    // 13.75px overlap instead.
    const reservation = ruleBody(
      '.workspace-screen[data-mobile-action-reserve="true"] .feature-main',
      workspaceStyles,
    );
    expect(reservation).toContain("padding-bottom");
    const askOverride = ruleBody(".workspace-screen.ask-screen .feature-main");
    expect(askOverride).toContain("padding-bottom: env(safe-area-inset-bottom)");
    // Specificity: two classes on the ancestor + one on .feature-main = three
    // class-level selectors, matching the shared rule's class+attribute+class.
    // A plain `.ask-screen .feature-main` (two) would lose the tie-break to
    // source order the other way and regress silently again.
    expect(askOverride).not.toBe("");
  });

});

function ruleBody(selector: string, source = styles) {
  return ruleBodies(selector, source).at(-1) ?? "";
}

function ruleBodies(selector: string, source = styles) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return [...source.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g"))].map(
    (match) => match[1] ?? "",
  );
}
