import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

describe("PageEditor static regressions", () => {
  it("separates the 1024px Context drawer from 640px single-pane editor behavior", () => {
    const source = readFileSync(
      join(process.cwd(), "src/features/pages/PageEditor.tsx"),
      "utf-8",
    );

    expect(source).toContain("const CONTEXT_RAIL_DRAWER_MAX_WIDTH = 1024");
    expect(source).toContain("const SINGLE_PANE_EDITOR_MAX_WIDTH = 640");
    expect(source).toContain("<MobileDrawer");
    expect(source).toContain("mobileLayout={contextRailDrawerViewport}");
    expect(source).toContain("active: contextRailDrawerViewport && contextRailOpen");
    expect(source).toContain("useMobileModalFocus({");
  });

  it("defers inline-toolbar sync after keyboard formatting dispatch", () => {
    const source = readFileSync(
      join(process.cwd(), "src/features/pages/PageEditor.tsx"),
      "utf-8",
    );

    expect(source).toContain("scheduleInlineToolbarSync(view)");
    expect(source).toContain("queueMicrotask");
  });

  it("keeps the Sprint 7 CodeMirror extension cluster enabled", () => {
    const source = readFileSync(
      join(process.cwd(), "src/features/pages/PageEditor.tsx"),
      "utf-8",
    );

    for (const extension of [
      "lineNumbers()",
      "highlightActiveLineGutter()",
      "foldGutter()",
      "syntaxHighlighting(defaultHighlightStyle",
      "bracketMatching()",
      "closeBrackets()",
      "highlightActiveLine()",
      "highlightSelectionMatches()",
      "...closeBracketsKeymap",
      "...searchKeymap",
      "...foldKeymap",
    ]) {
      expect(source).toContain(extension);
    }
  });

  it("positions the slash block menu through floating-ui", () => {
    const source = readFileSync(
      join(process.cwd(), "src/features/pages/PageEditor.tsx"),
      "utf-8",
    );

    expect(source).toContain("@floating-ui/dom");
    expect(source).toContain("computePosition(reference, floating");
    expect(source).toContain("flip({ padding: INLINE_TOOLBAR_MARGIN })");
    expect(source).toContain("shift({ padding: INLINE_TOOLBAR_MARGIN })");
    expect(source).toContain("strategy: \"fixed\"");
  });

  it("keeps the editor empty-state hint tied to slash blocks", () => {
    const source = readFileSync(
      join(process.cwd(), "src/features/pages/PageEditor.tsx"),
      "utf-8",
    );

    expect(source).toContain("Start typing… (Press / for blocks)");
  });

  it("keeps Sprint 7 mobile tap target guard rails in CSS", () => {
    const styles = readAppStyles();

    expect(styles).toContain(".editor-toolbar-button[aria-pressed=\"true\"]");
    expect(styles).toContain(".tree-row-chevron");
    expect(styles).toContain("min-width: 44px");
    expect(styles).toContain("min-height: 44px");
  });

  it("keeps the editor mode switcher as the right-aligned command pill", () => {
    const styles = readAppStyles();

    expect(styles).toContain(".editor-mode-tabs");
    expect(styles).toContain("margin-left: auto");
    expect(styles).toContain("border-radius: 999px");
  });

  it("gives split panes bounded tracks and distinct menu interaction states", () => {
    const styles = readAppStyles();

    expect(styles).toContain("minmax(320px, var(--editor-split-start, 50fr))");
    expect(styles).toContain("minmax(320px, var(--editor-split-end, 50fr))");
    expect(styles).toContain("minmax(280px, var(--editor-split-start, 50fr))");
    expect(styles).toContain(":not([aria-disabled=\"true\"])");
    expect(styles).toContain(".editor-action-button.active:focus");
    expect(styles).toContain("inset 4px 0 0 var(--context)");
  });

  it("keeps arbitrary exception and API warning text out of user-visible editor status", () => {
    const source = readFileSync(
      join(process.cwd(), "src/features/pages/PageEditor.tsx"),
      "utf-8",
    );

    expect(source).toContain("apiErrorMessage(");
    expect(source).not.toContain("exc.message");
    expect(source).not.toContain("defaultValue: warning.message");
  });

  it("keeps markdown list markers and inline code visibly styled", () => {
    const styles = readAppStyles();

    expect(styles).toContain(".markdown-body ul {");
    expect(styles).toContain("list-style-type: disc");
    expect(styles).toContain(".markdown-body ol {");
    expect(styles).toContain("list-style-type: decimal");
    expect(styles).toContain(".markdown-body li::marker");
    expect(styles).toContain(".markdown-body :not(pre) > code");
    expect(styles).toContain("background: color-mix(in srgb, var(--surface-active) 84%, white)");
    expect(styles).toContain("border: 1px solid color-mix(in srgb, var(--border) 80%, transparent)");
  });
});
