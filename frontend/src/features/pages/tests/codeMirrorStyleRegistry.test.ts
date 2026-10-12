import { EditorView } from "@codemirror/view";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  documentStyleRuleCount,
  resetCodeMirrorStyleRegistry,
} from "./codeMirrorStyleRegistry";

// This is the canary for the helper PageEditor.test.tsx relies on: it reaches
// into style-mod's per-document registry by the symbol's description, so a
// style-mod release that renames or hides it would otherwise turn the helper
// into a silent no-op and bring the suite's slowdown back without a failure.
describe("resetCodeMirrorStyleRegistry", () => {
  const views: EditorView[] = [];

  function mountView(extraRule?: string) {
    const parent = document.createElement("div");
    document.body.appendChild(parent);
    const view = new EditorView({
      doc: "alpha",
      extensions: extraRule
        ? [EditorView.theme({ [extraRule]: { color: "red" } })]
        : [],
      parent,
    });
    views.push(view);
    return view;
  }

  beforeEach(() => {
    resetCodeMirrorStyleRegistry();
  });

  afterEach(() => {
    for (const view of views.splice(0)) {
      view.dom.parentElement?.remove();
      view.destroy();
    }
    resetCodeMirrorStyleRegistry();
  });

  it("drops every rule earlier views left behind and lets the next view mount its own", () => {
    mountView();
    const baseline = documentStyleRuleCount();
    expect(baseline).toBeGreaterThan(0);

    // A module created per mount, the way @uiw/react-codemirror creates its
    // default theme per render, is mounted next to the earlier rules and stays.
    mountView(".cm-registry-canary-a");
    mountView(".cm-registry-canary-b");
    expect(documentStyleRuleCount()).toBeGreaterThan(baseline);

    resetCodeMirrorStyleRegistry();
    expect(documentStyleRuleCount()).toBe(0);
    expect(document.head.querySelectorAll("style")).toHaveLength(0);
    expect(
      Object.getOwnPropertySymbols(document).filter(
        (key) => key.description === "styleSet",
      ),
    ).toHaveLength(0);

    mountView();
    expect(documentStyleRuleCount()).toBe(baseline);
  });
});
