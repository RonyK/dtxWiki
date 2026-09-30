import { history, redo, undo, undoDepth } from "@codemirror/commands";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { describe, expect, it } from "vitest";

import {
  addEditorUploadSlotsAtRange,
  clearEditorUploadSlots,
  editorUploadSlotsExtension,
  getEditorUploadSlot,
  getEditorUploadSlots,
  removeEditorUploadSlot,
  resolveEditorUploadSlot,
} from "../editorUploadSlots";

function createView(doc: string, selection?: { anchor: number; head?: number }) {
  return new EditorView({
    state: EditorState.create({
      doc,
      selection,
      extensions: [history(), editorUploadSlotsExtension],
    }),
    parent: document.body,
  });
}

describe("editor upload slots", () => {
  it("keeps pending selected text, selection, and undo history canonical", () => {
    const view = createView("before selected after", { anchor: 7, head: 15 });

    try {
      const beforeDepth = undoDepth(view.state);
      addEditorUploadSlotsAtRange(
        view,
        { from: 7, to: 15 },
        [
          { group: "batch-a", id: "a", ordinal: 0 },
          { group: "batch-a", id: "b", ordinal: 1 },
        ],
      );

      expect(view.state.doc.toString()).toBe("before selected after");
      expect(view.state.selection.main).toMatchObject({ from: 7, to: 15 });
      expect(undoDepth(view.state)).toBe(beforeDepth);
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);
      expect(getEditorUploadSlots(view.state)).toEqual([
        { association: -1, from: 7, group: "batch-a", id: "a", ordinal: 0, to: 15 },
        { association: -1, from: 7, group: "batch-a", id: "b", ordinal: 1, to: 15 },
      ]);
    } finally {
      view.destroy();
    }
  });

  it("maps a caret under boundary typing and drops it when deletion crosses it", () => {
    const view = createView("0123456789");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 5, to: 5 },
        [{ group: "batch-a", id: "a", ordinal: 0 }],
      );
      view.dispatch({ changes: { from: 5, insert: "typed" } });
      expect(getEditorUploadSlot(view.state, "a")).toMatchObject({ from: 5, to: 5 });

      view.dispatch({ changes: { from: 4, to: 7, insert: "x" } });
      expect(getEditorUploadSlot(view.state, "a")).toBeNull();
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);
    } finally {
      view.destroy();
    }
  });

  it("maps selected-range boundary edits and invalidates an interior edit", () => {
    const view = createView("0123456789");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 2, to: 6 },
        [{ group: "batch-a", id: "a", ordinal: 0 }],
      );
      view.dispatch({ changes: { from: 2, insert: "L" } });
      expect(getEditorUploadSlot(view.state, "a")).toMatchObject({ from: 3, to: 7 });
      view.dispatch({ changes: { from: 4, insert: "inside" } });
      expect(getEditorUploadSlot(view.state, "a")).toBeNull();
    } finally {
      view.destroy();
    }
  });

  it("preserves text typed at both selected-range boundaries on completion", () => {
    const view = createView("before selected after");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 7, to: 15 },
        [{ group: "batch-a", id: "a", ordinal: 0 }],
      );
      view.dispatch({ changes: { from: 7, insert: "LEFT" } });
      const afterLeft = getEditorUploadSlot(view.state, "a");
      expect(afterLeft).toMatchObject({ from: 11, to: 19 });
      view.dispatch({ changes: { from: afterLeft?.to ?? 0, insert: "RIGHT" } });
      expect(getEditorUploadSlot(view.state, "a")).toMatchObject({ from: 11, to: 19 });

      resolveEditorUploadSlot(view, "a", "![a](dtx-media://asset/a)");
      const body = view.state.doc.toString();
      expect(body).toContain("LEFT");
      expect(body).toContain("RIGHT");
      expect(body).not.toContain("selected");
    } finally {
      view.destroy();
    }
  });

  it("keeps A-E FileList order when a shared selected range completes inversely", () => {
    const view = createView("before selected after");

    try {
      const ids = ["a", "b", "c", "d", "e"];
      addEditorUploadSlotsAtRange(
        view,
        { from: 7, to: 15 },
        ids.map((id, ordinal) => ({ group: "batch-a", id, ordinal })),
      );
      for (const id of [...ids].reverse()) {
        expect(resolveEditorUploadSlot(
          view,
          id,
          `![${id}](dtx-media://asset/${id})`,
        )).toBe(true);
      }

      const body = view.state.doc.toString();
      for (let index = 1; index < ids.length; index += 1) {
        expect(body.indexOf(`![${ids[index - 1]}]`))
          .toBeLessThan(body.indexOf(`![${ids[index]}]`));
      }
      expect(body).not.toContain("selected");
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);
    } finally {
      view.destroy();
    }
  });

  it("preserves insertion order across batches sharing one selected range", () => {
    const view = createView("before selected after");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 7, to: 15 },
        [{ group: "batch-first", id: "first", ordinal: 0 }],
      );
      addEditorUploadSlotsAtRange(
        view,
        { from: 7, to: 15 },
        [{ group: "batch-second", id: "second", ordinal: 0 }],
      );
      resolveEditorUploadSlot(view, "second", "![second](dtx-media://asset/second)");
      resolveEditorUploadSlot(view, "first", "![first](dtx-media://asset/first)");

      const body = view.state.doc.toString();
      expect(body.indexOf("![first]")).toBeLessThan(body.indexOf("![second]"));
      expect(body).not.toContain("selected");
    } finally {
      view.destroy();
    }
  });

  it("invalidates a partially-overlapping sibling when the first range resolves", () => {
    const view = createView("0123456789");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 2, to: 6 },
        [{ group: "batch-a", id: "a", ordinal: 0 }],
      );
      addEditorUploadSlotsAtRange(
        view,
        { from: 4, to: 8 },
        [{ group: "batch-b", id: "b", ordinal: 0 }],
      );
      expect(resolveEditorUploadSlot(view, "a", "![a](dtx-media://asset/a)")).toBe(true);
      expect(getEditorUploadSlot(view.state, "b")).toBeNull();
      expect(resolveEditorUploadSlot(view, "b", "![b](dtx-media://asset/b)")).toBe(false);
    } finally {
      view.destroy();
    }
  });

  it("makes remove and clear non-historical and non-resurrecting", () => {
    const view = createView("Canonical");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 9, to: 9 },
        [
          { group: "batch-a", id: "remove", ordinal: 0 },
          { group: "batch-a", id: "clear", ordinal: 1 },
        ],
      );
      const beforeDepth = undoDepth(view.state);
      expect(removeEditorUploadSlot(view, "remove")).toBe(true);
      expect(clearEditorUploadSlots(view)).toBe(true);
      expect(undoDepth(view.state)).toBe(beforeDepth);
      expect(undo(view)).toBe(false);
      expect(redo(view)).toBe(false);
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);
      expect(view.state.doc.toString()).toBe("Canonical");
    } finally {
      view.destroy();
    }
  });

  it("undoes and redoes one selected-range success without transient state", () => {
    const view = createView("before selected after");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 7, to: 15 },
        [{ group: "batch-a", id: "complete", ordinal: 0 }],
      );
      resolveEditorUploadSlot(
        view,
        "complete",
        "![complete](dtx-media://asset/complete)",
      );
      expect(view.state.doc.toString()).toContain("dtx-media://asset/complete");
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);

      expect(undo(view)).toBe(true);
      expect(view.state.doc.toString()).toBe("before selected after");
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);
      expect(redo(view)).toBe(true);
      expect(view.state.doc.toString()).toContain("dtx-media://asset/complete");
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);
    } finally {
      view.destroy();
    }
  });

  it("keeps a pending earlier sibling before restored source when success is undone", () => {
    const view = createView("before selected after");

    try {
      addEditorUploadSlotsAtRange(
        view,
        { from: 7, to: 15 },
        [
          { group: "batch-a", id: "first", ordinal: 0 },
          { group: "batch-a", id: "second", ordinal: 1 },
        ],
      );
      resolveEditorUploadSlot(view, "second", "![second](dtx-media://asset/second)");
      expect(undo(view)).toBe(true);
      expect(view.state.doc.toString()).toBe("before selected after");
      expect(getEditorUploadSlot(view.state, "first")).toMatchObject({
        association: -1,
        from: 7,
        to: 7,
      });

      resolveEditorUploadSlot(view, "first", "![first](dtx-media://asset/first)");
      const body = view.state.doc.toString();
      expect(body.indexOf("![first]")).toBeLessThan(body.indexOf("selected"));
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);
    } finally {
      view.destroy();
    }
  });
});
