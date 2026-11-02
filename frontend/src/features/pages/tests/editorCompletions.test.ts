import {
  acceptCompletion,
  autocompletion,
  closeBrackets,
  insertBracket,
  pickedCompletion,
  currentCompletions,
  startCompletion,
  type CompletionContext,
  type Completion,
} from "@codemirror/autocomplete";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { createEditorCompletionSource, isLikelyUuid } from "../editorCompletions";

describe("editor completions", () => {
  it("suggests stable page links from loaded pages and keeps slugs searchable", () => {
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
      { current: "workspace-1" },
    );

    const result = source(completionContext("[[release"));

    expect(result?.from).toBe(2);
    expect(result?.options).toContainEqual(
      expect.objectContaining({
        apply: expect.any(Function),
        displayLabel: "Roadmap",
        detail: "release-notes",
        label: "Roadmap release-notes",
      }),
    );
  });

  it("accepts a typed slug completion and replaces the whole trigger with a literal-title permalink", async () => {
    const title = 'Road[map] \\ *draft* _note_ `code` <x> ! &amp; ~old~';
    const source = createEditorCompletionSource(
      {
        current: [
          {
            id: "p1",
            slug: "release-notes",
            title,
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
      { current: "workspace-1" },
    );
    const doc = "See ";
    let pickedLabel: string | undefined;
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({
        doc,
        selection: { anchor: doc.length },
        extensions: [
          closeBrackets(),
          autocompletion({ override: [source], interactionDelay: 0 }),
          EditorView.updateListener.of(({ transactions }) => {
            pickedLabel = transactions[0]?.annotation(pickedCompletion)?.displayLabel ?? pickedLabel;
          }),
        ],
      }),
    });
    try {
      view.dispatch(insertBracket(view.state, "[")!);
      view.dispatch(insertBracket(view.state, "[")!);
      expect(view.state.doc.toString()).toBe("See [[]]");
      expect(startCompletion(view)).toBe(true);
      await waitFor(() => expect(currentCompletions(view.state)).toHaveLength(1));
      const queryStart = view.state.selection.main.head;
      view.dispatch({
        changes: { from: queryStart, insert: "release" },
        selection: { anchor: queryStart + "release".length },
        userEvent: "input.type",
      });
      expect(currentCompletions(view.state)).toHaveLength(1);
      expect(acceptCompletion(view)).toBe(true);
      expect(pickedLabel).toBe(title);
      expect(view.state.doc.toString()).toBe(
        'See [Road\\[map\\] \\\\ \\*draft\\* \\_note\\_ \\`code\\` \\<x\\> \\! \\&amp; \\~old\\~](/p/workspace-1/p1)',
      );
      // The picker does not intercept hand-authored legacy wikilinks.
      expect(source(completionContext("[[release-notes|Legacy]]"))).toBeNull();
    } finally {
      view.destroy();
      view.dom.remove();
    }
  });

  it("suggests readable citation references and inserts document links", () => {
    const source = createEditorCompletionSource(
      { current: [] },
      {
        current: [
          sourceSummary("src-1", "API Design Source", "https://example.com/api"),
          sourceSummary("src-2", "API Design Source", "https://example.com/other"),
        ],
      },
      { current: "workspace-1" },
    );

    const result = source(completionContext("@cite{api design"));

    expect(result?.from).toBe("@cite{".length);
    expect(result?.options).toContainEqual(
      expect.objectContaining({
        apply: expect.any(Function),
        detail: "Library",
        label: "API Design Source",
      }),
    );
    expect(result?.options).toContainEqual(
      expect.objectContaining({
        apply: expect.any(Function),
        label: "API Design Source",
      }),
    );
  });

  it("isLikelyUuid detects standard UUID v4 shape", () => {
    expect(isLikelyUuid("550e8400-e29b-41d4-a716-446655440000")).toBe(true);
    expect(isLikelyUuid("src-design-notes")).toBe(false);
    expect(isLikelyUuid("a1b2c3")).toBe(false);
    expect(isLikelyUuid("")).toBe(false);
  });

  it("uses stable Library document identity across title and URI changes", () => {
    const sources = { current: [sourceSummary("550e8400-e29b-41d4-a716-446655440000", "Original", "https://example.com/original")] };
    const source = createEditorCompletionSource({ current: [] }, sources, { current: "workspace-1" });
    const first = source(completionContext("@cite{"));
    sources.current = [sourceSummary("550e8400-e29b-41d4-a716-446655440000", "Renamed", "https://example.com/renamed")];
    const second = source(completionContext("@cite{"));
    expect(applyCitation(first!.options[0])).toBe("@cite{/workspaces/workspace-1/library/items/550e8400-e29b-41d4-a716-446655440000}");
    expect(applyCitation(second!.options[0])).toBe(applyCitation(first!.options[0]));
    expect(second?.options[0].label).toBe("Renamed");
  });

  it("offers the shared document picker for the /cite command", () => {
    const open = vi.fn();
    const source = createEditorCompletionSource({ current: [] }, { current: [] }, { current: "workspace-1" }, { label: "Choose reference", open });
    const result = source(completionContext("/cite"));
    expect(result?.options).toHaveLength(1);
    expect(result?.options[0].label).toBe("Choose reference");
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

function applyCitation(completion: Completion): string {
  const view = new EditorView({ parent: document.body, state: EditorState.create({ doc: "@cite{}", selection: { anchor: 6 } }) });
  try {
    if (typeof completion.apply !== "function") throw new Error("Expected interactive citation completion");
    completion.apply(view, completion, 6, 6);
    return view.state.doc.toString();
  } finally { view.destroy(); view.dom.remove(); }
}
