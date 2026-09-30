import { isolateHistory } from "@codemirror/commands";
import {
  type EditorState,
  type Extension,
  StateEffect,
  StateField,
  Transaction,
  type TransactionSpec,
} from "@codemirror/state";
import type { EditorView } from "@codemirror/view";

export type EditorUploadSlotSeed = Readonly<{
  group: string;
  id: string;
  ordinal: number;
}>;

export type EditorUploadSlot = EditorUploadSlotSeed & Readonly<{
  association: -1 | 1;
  from: number;
  to: number;
}>;

type AddSlotsEffect = Readonly<{
  from: number;
  slots: readonly EditorUploadSlotSeed[];
  to: number;
}>;

const addSlotsEffect = StateEffect.define<AddSlotsEffect>();
const resolveSlotEffect = StateEffect.define<string>();
const removeSlotEffect = StateEffect.define<string>();
const clearSlotsEffect = StateEffect.define<null>();

/**
 * Editor-owned ranges for in-flight media uploads.
 *
 * Pending uploads never change `EditorState.doc` or its history. The original
 * selection remains canonical until a successful upload replaces that range
 * with one logical reference. This prevents Undo/Redo, removal, or authority
 * invalidation from resurrecting an upload task or deleting authored text.
 */
export const editorUploadSlotsState = StateField.define<readonly EditorUploadSlot[]>({
  create: () => [],
  update(slots, transaction) {
    const resolving = resolvingSlot(slots, transaction);
    let next: readonly EditorUploadSlot[] = slots.flatMap((slot, index) => {
      if (changeInvalidatesSlot(slot, transaction, resolving)) {
        return [];
      }
      return [mapSlot(slot, index, transaction, resolving)];
    });

    for (const effect of transaction.effects) {
      if (effect.is(clearSlotsEffect)) {
        next = [];
        continue;
      }
      if (effect.is(resolveSlotEffect) || effect.is(removeSlotEffect)) {
        next = next.filter((slot) => slot.id !== effect.value);
        continue;
      }
      if (effect.is(addSlotsEffect)) {
        next = addSlots(next, effect.value);
      }
    }
    return next;
  },
});

export const editorUploadSlotsExtension: Extension = editorUploadSlotsState;

export function getEditorUploadSlots(state: EditorState) {
  return state.field(editorUploadSlotsState, false) ?? [];
}

export function getEditorUploadSlot(state: EditorState, id: string) {
  return getEditorUploadSlots(state).find((slot) => slot.id === id) ?? null;
}

export function countEditorUploadSlots(state: EditorState) {
  return getEditorUploadSlots(state).length;
}

export function hasEditorUploadSlot(view: EditorView, id: string) {
  return getEditorUploadSlot(view.state, id) !== null;
}

export function editorUploadSlotWasResolved(
  transactions: readonly Transaction[],
  id: string,
) {
  return transactions.some((transaction) =>
    transaction.effects.some(
      (effect) => effect.is(resolveSlotEffect) && effect.value === id,
    ));
}

/**
 * Creates transient anchors without changing the document, selection, or undo
 * history. Callers must pass the already-normalized safe block range.
 */
export function addEditorUploadSlotsAtRange(
  view: EditorView,
  requestedRange: { from: number; to: number },
  seeds: readonly EditorUploadSlotSeed[],
) {
  requireSlotField(view.state);
  if (seeds.length === 0) return false;
  validateSeeds(view.state, seeds);

  const documentLength = view.state.doc.length;
  const from = Math.max(0, Math.min(requestedRange.from, documentLength));
  const to = Math.max(from, Math.min(requestedRange.to, documentLength));
  dispatchTransientEffect(view, addSlotsEffect.of({ from, slots: seeds, to }));
  return true;
}

/**
 * Replaces the anchored range with one completed logical reference and removes
 * the slot in the same isolated history transaction. Exact-range siblings are
 * collapsed before or after this insertion so inverse completion preserves
 * FileList and batch insertion order without consuming the source twice.
 */
export function resolveEditorUploadSlot(
  view: EditorView,
  id: string,
  markdown: string,
) {
  const slot = getEditorUploadSlot(view.state, id);
  if (!slot) return false;
  const attachment = markdown.trim();
  if (!attachment) {
    throw new Error("A completed upload requires non-empty Markdown.");
  }
  const documentLength = view.state.doc.length;
  const from = Math.max(0, Math.min(slot.from, documentLength));
  const to = Math.max(from, Math.min(slot.to, documentLength));
  const insert = normalizedBlockAttachment(
    view.state.doc.toString(),
    from,
    to,
    attachment,
  );
  view.dispatch({
    annotations: isolateHistory.of("full"),
    changes: { from, to, insert },
    effects: resolveSlotEffect.of(id),
    scrollIntoView: true,
  });
  return true;
}

/** Removes only transient editor state; it never rewrites canonical Markdown. */
export function removeEditorUploadSlot(view: EditorView, id: string) {
  if (!hasEditorUploadSlot(view, id)) return false;
  dispatchTransientEffect(view, removeSlotEffect.of(id));
  return true;
}

export function clearEditorUploadSlots(view: EditorView) {
  if (getEditorUploadSlots(view.state).length === 0) return false;
  dispatchTransientEffect(view, clearSlotsEffect.of(null));
  return true;
}

function dispatchTransientEffect(
  view: EditorView,
  effect: StateEffect<unknown>,
) {
  const spec: TransactionSpec = {
    annotations: Transaction.addToHistory.of(false),
    effects: effect,
  };
  view.dispatch(spec);
}

function resolvingSlot(
  slots: readonly EditorUploadSlot[],
  transaction: Transaction,
) {
  for (const effect of transaction.effects) {
    if (effect.is(resolveSlotEffect)) {
      const index = slots.findIndex((slot) => slot.id === effect.value);
      return index < 0 ? null : { index, slot: slots[index] };
    }
  }
  return null;
}

function mapSlot(
  slot: EditorUploadSlot,
  index: number,
  transaction: Transaction,
  resolving: { index: number; slot: EditorUploadSlot } | null,
): EditorUploadSlot {
  if (resolving && slot.id !== resolving.slot.id &&
      sameRange(slot, resolving.slot)) {
    const association = insertionSide(slot, index, resolving);
    const boundary = association < 0 ? slot.from : slot.to;
    const position = transaction.changes.mapPos(boundary, association);
    return Object.freeze({ ...slot, association, from: position, to: position });
  }

  if (slot.from === slot.to) {
    const position = transaction.changes.mapPos(slot.from, slot.association);
    return Object.freeze({ ...slot, from: position, to: position });
  }
  // Boundary insertions are authored outside the originally selected text:
  // start sticks after left-boundary input and end sticks before right-boundary
  // input, so a later successful replacement never deletes those edits.
  const from = transaction.changes.mapPos(slot.from, 1);
  const to = transaction.changes.mapPos(slot.to, -1);
  return Object.freeze({ ...slot, from: Math.min(from, to), to: Math.max(from, to) });
}

function changeInvalidatesSlot(
  slot: EditorUploadSlot,
  transaction: Transaction,
  resolving: { index: number; slot: EditorUploadSlot } | null,
) {
  if (!transaction.docChanged || resolving?.slot.id === slot.id ||
      (resolving && sameRange(slot, resolving.slot))) {
    return false;
  }
  let invalidated = false;
  transaction.changes.iterChangedRanges((fromA, toA) => {
    if (invalidated) return;
    if (slot.from === slot.to) {
      invalidated = fromA < slot.from && toA > slot.to;
      return;
    }
    invalidated = fromA === toA
      ? fromA > slot.from && fromA < slot.to
      : fromA < slot.to && toA > slot.from;
  });
  return invalidated;
}

function insertionSide(
  slot: EditorUploadSlot,
  index: number,
  resolving: { index: number; slot: EditorUploadSlot },
): -1 | 1 {
  if (slot.group === resolving.slot.group) {
    return slot.ordinal < resolving.slot.ordinal ? -1 : 1;
  }
  return index < resolving.index ? -1 : 1;
}

function sameRange(left: EditorUploadSlot, right: EditorUploadSlot) {
  return left.from === right.from && left.to === right.to;
}

function addSlots(
  current: readonly EditorUploadSlot[],
  addition: AddSlotsEffect,
) {
  const next = [...current];
  for (const seed of addition.slots) {
    next.push(Object.freeze({
      ...seed,
      association: -1 as const,
      from: addition.from,
      to: addition.to,
    }));
  }
  return Object.freeze(next);
}

function validateSeeds(
  state: EditorState,
  seeds: readonly EditorUploadSlotSeed[],
) {
  const ids = new Set(getEditorUploadSlots(state).map((slot) => slot.id));
  const ordinals = new Set(
    getEditorUploadSlots(state).map((slot) => `${slot.group}\u0000${slot.ordinal}`),
  );
  for (const seed of seeds) {
    if (!seed.id.trim() || !seed.group.trim() ||
        !Number.isSafeInteger(seed.ordinal) || seed.ordinal < 0) {
      throw new Error("Upload slot metadata is invalid.");
    }
    const ordinalKey = `${seed.group}\u0000${seed.ordinal}`;
    if (ids.has(seed.id) || ordinals.has(ordinalKey)) {
      throw new Error("Upload slot metadata must be unique.");
    }
    ids.add(seed.id);
    ordinals.add(ordinalKey);
  }
}

function requireSlotField(state: EditorState) {
  if (!state.field(editorUploadSlotsState, false)) {
    throw new Error("The editor upload slots extension is not installed.");
  }
}

function normalizedBlockAttachment(
  documentText: string,
  from: number,
  to: number,
  attachment: string,
) {
  const before = documentText.slice(0, from);
  const after = documentText.slice(to);
  return `${blockSeparatorBefore(before)}${attachment}${blockSeparatorAfter(after)}`;
}

function blockSeparatorBefore(before: string) {
  if (!before) return "";
  if (before.endsWith("\n\n")) return "";
  return before.endsWith("\n") ? "\n" : "\n\n";
}

function blockSeparatorAfter(after: string) {
  if (!after) return "\n";
  if (after.startsWith("\n\n")) return "";
  return after.startsWith("\n") ? "\n" : "\n\n";
}
