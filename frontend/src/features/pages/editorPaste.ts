import type { EditorView as EditorViewType } from "@codemirror/view";
import { EditorView } from "@codemirror/view";

import { apiErrorMessage } from "../../shared/api/client";
import { contextMediaAssetIdFromReference } from "../../shared/api/contextMedia";
import { ImageUploadApiError } from "../../shared/api/uploads";
import { isAbortError as isUploadQueueAbortError } from "../../shared/lib/abortError";
import {
  type ResilientUploadQueueStatus,
  UploadQueue,
} from "../../shared/media/uploadQueue";
import {
  boundedMediaUploadRetryDelayMs,
  createMediaUploadCoordinatorState,
  decideMediaUploadPolicyRepreflight,
  type MediaUploadBatchCapabilities,
  type MediaUploadCandidate,
  type MediaUploadCoordinatorState,
  type MediaUploadPreflight,
  type MediaUploadRejectionCode,
  isMediaPolicyChangedError,
  preflightMediaUploadBatch,
  reduceMediaUploadCoordinator,
  resolveMediaUploadBatchLimits,
} from "./mediaUploadCoordinator";
import {
  addEditorUploadSlotsAtRange,
  editorUploadSlotWasResolved,
  editorUploadSlotsExtension,
  getEditorUploadSlots,
  hasEditorUploadSlot,
  removeEditorUploadSlot,
  resolveEditorUploadSlot,
} from "./editorUploadSlots";

type ClipboardLike = {
  files?: ArrayLike<File>;
  getData(type: string): string;
};

export type ImageUploadResult = {
  logical_ref: string;
  asset_id?: string;
  content_url?: string;
};

export type ImageUploadContext = {
  editingVersionId: string;
  marker: string;
  pageId: string;
  signal: AbortSignal;
  targetSnapshot: UploadTargetSnapshot;
  workspaceId: string;
};

export type ImageUploadFn = (
  file: File,
  context: ImageUploadContext,
) => Promise<ImageUploadResult>;

export type ImageUploadLifecycleStatus = ResilientUploadQueueStatus;

export type UploadPayloadFingerprint = Readonly<{
  fileName: string;
  lastModified: number;
  mediaType: string;
  size: number;
}>;

export type UploadTargetSnapshot = Readonly<{
  actorSessionGeneration: number;
  assetOperationId: string;
  editingVersionId: string;
  editorGeneration: number;
  pageId: string;
  payloadFingerprint: UploadPayloadFingerprint;
  policyRevision: string;
  workspaceId: string;
}>;

export type EditorPasteOptions = {
  createUploadTargetSnapshot?: (
    file: File,
    operationId: string,
    signal: AbortSignal,
  ) => Promise<UploadTargetSnapshot> | UploadTargetSnapshot;
  isUploadTargetCurrent?: (snapshot: UploadTargetSnapshot) => boolean;
  getUploadBatchCapabilities?: () => MediaUploadBatchCapabilities;
  isUploadCapabilityCurrent?: () => boolean;
  onUploadBatchesChange?: (batches: readonly MediaUploadCoordinatorState[]) => void;
  onUploadError?: (
    file: File,
    message: string,
    rejectionCode?: MediaUploadRejectionCode,
  ) => void;
  onUploadSettled?: (file: File) => void;
  onUploadStart?: (file: File) => void;
  onUploadStateChange?: (
    upload: ImageUploadPlaceholder,
    status: ImageUploadLifecycleStatus,
    message?: string,
  ) => void;
  refreshUploadPolicy?: (
    stalePolicyRevision: string,
  ) => Promise<MediaUploadBatchCapabilities>;
  uploadCapabilitiesUnavailableMessage?: () => string;
  uploadInsertionInvalidatedMessage?: () => string;
  uploadImage?: ImageUploadFn;
  validateImageFile?: (file: File) => string | null;
};

export type ImageUploadPlaceholder = {
  file: File;
  insertionSlotAvailable?: boolean;
  marker: string;
  name: string;
  slotGroup: string;
  slotOrdinal: number;
};

type EditorUploadTask = {
  batchId: string | null;
  queuedAnnounced: boolean;
  options: EditorPasteOptions;
  snapshot: UploadTargetSnapshot;
  upload: ImageUploadPlaceholder;
  view: EditorViewType;
};

type EditorUploadPreparation = {
  batchId: string | null;
  controller: AbortController;
  options: EditorPasteOptions;
  silent: boolean;
  terminalNotified: boolean;
  upload: ImageUploadPlaceholder;
  view: EditorViewType;
};

type EditorUploadBatch = {
  batchId: string;
  freshCapabilities: MediaUploadBatchCapabilities | null;
  options: EditorPasteOptions;
  originalCandidates: MediaUploadCandidate[];
  placeholders: Map<string, ImageUploadPlaceholder>;
  preparingIds: Set<string>;
  refreshPromise: Promise<void> | null;
  stalePolicyRevision: string | null;
  state: MediaUploadCoordinatorState;
  view: EditorViewType | null;
};

const uploadQueues = new WeakMap<
  EditorPasteOptions,
  UploadQueue<EditorUploadTask, true>
>();
const uploadBatches = new WeakMap<
  EditorPasteOptions,
  Map<string, EditorUploadBatch>
>();
const uploadPreparations = new WeakMap<
  EditorPasteOptions,
  Map<string, EditorUploadPreparation>
>();
let uploadBatchSequence = 0;
let uploadSlotGroupSequence = 0;

export function createUploadTargetSnapshot({
  actorSessionGeneration,
  editingVersionId,
  editorGeneration,
  file,
  operationId,
  pageId,
  policyRevision,
  workspaceId,
}: {
  actorSessionGeneration: number;
  editingVersionId: string;
  editorGeneration: number;
  file: File;
  operationId: string;
  pageId: string;
  policyRevision: string;
  workspaceId: string;
}): UploadTargetSnapshot {
  if (!operationId.trim()) {
    throw new Error("Upload operation id is not available.");
  }
  validateUploadTargetMetadata({
    actorSessionGeneration,
    editingVersionId,
    editorGeneration,
    pageId,
    policyRevision,
    workspaceId,
  });
  const payloadFingerprint = payloadFingerprintForFile(file);
  return Object.freeze({
    actorSessionGeneration,
    assetOperationId: operationId,
    editingVersionId,
    editorGeneration,
    pageId,
    payloadFingerprint,
    policyRevision,
    workspaceId,
  });
}

export function createEditorPasteHandlers(options: EditorPasteOptions = {}) {
  return [
    editorUploadSlotsExtension,
    EditorView.updateListener.of((update) => {
      const currentIds = new Set(getEditorUploadSlots(update.state).map((slot) => slot.id));
      for (const slot of getEditorUploadSlots(update.startState)) {
        if (currentIds.has(slot.id) ||
            editorUploadSlotWasResolved(update.transactions, slot.id)) continue;
        // A selected upload range was edited, explicitly removed, or cleared
        // by an authority transition. Its task can no longer be replayed at
        // the original insertion point, so retain Remove-only UI state.
        invalidateRemovedUploadSlot(options, slot.id);
      }
    }),
    EditorView.domEventHandlers({
    paste(event, view) {
      const imageSelection = selectImageUploads(event.clipboardData?.files, options);
      if (imageSelection.handled) {
        event.preventDefault();
        if (imageSelection.placeholders.length > 0) {
          addImageUploadSlotsAtRange(
            view,
            view.state.selection.main,
            imageSelection.placeholders,
          );
          startImageUploads(
            view,
            imageSelection.placeholders,
            options,
            imageSelection.batchId,
          );
        }
        return true;
      }
      const text = markdownFromClipboard(event.clipboardData);
      if (!text) {
        return false;
      }
      event.preventDefault();
      insertAtSelection(view, text);
      return true;
    },
    drop(event, view) {
      const imageSelection = selectImageUploads(event.dataTransfer?.files, options);
      if (!imageSelection.handled) {
        return false;
      }
      event.preventDefault();
      if (imageSelection.placeholders.length === 0) {
        return true;
      }
      const position = event.clientX || event.clientY
        ? view.posAtCoords({ x: event.clientX, y: event.clientY })
        : null;
      addImageUploadSlotsAtRange(
        view,
        {
          from: position ?? view.state.selection.main.head,
          to: position ?? view.state.selection.main.head,
        },
        imageSelection.placeholders,
      );
      startImageUploads(
        view,
        imageSelection.placeholders,
        options,
        imageSelection.batchId,
      );
      return true;
    },
    }),
  ];
}

export function markdownFromClipboard(data: ClipboardLike | null) {
  if (!data) {
    return null;
  }
  const html = data.getData("text/html");
  if (!html.trim()) {
    return null;
  }
  // Proposal §7: when the clipboard carries both `text/html` and `text/plain`
  // and the HTML adds no block-level structure (no headings/lists/tables/code/
  // links/images), assume the user pasted plain text from a styled source
  // (Slack, Word, code editor) and bypass HTML→markdown conversion. Otherwise
  // the trip through htmlToMarkdown turns harmless text into noisy escapes.
  // Review C2.14.
  const plain = data.getData("text/plain");
  if (plain && !htmlAddsStructure(html, plain)) {
    return null;
  }
  const markdown = htmlToMarkdown(html).trim();
  return markdown ? `${markdown}\n` : null;
}

const STRUCTURAL_TAG_PATTERN =
  /<(h[1-6]|ul|ol|li|table|tr|td|th|pre|code|blockquote|hr|img|a|strong|b|em|i|del|s|p|br)\b/i;

function htmlAddsStructure(html: string, plain: string): boolean {
  // Quick reject: any structural tag present → keep HTML path.
  if (STRUCTURAL_TAG_PATTERN.test(html)) {
    return true;
  }
  // No structural tags — html is essentially a styled wrapper. If the plain
  // text equals the html text content (whitespace-normalized), bypass HTML.
  const stripped = html.replace(/<[^>]+>/g, "").replace(/&nbsp;/g, " ");
  return normalizeWhitespaceForCompare(stripped) !== normalizeWhitespaceForCompare(plain);
}

function normalizeWhitespaceForCompare(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

export function htmlToMarkdown(html: string) {
  const parser = new DOMParser();
  const document = parser.parseFromString(html, "text/html");
  return normalizeBlankLines(renderChildren(document.body));
}

function insertAtSelection(view: EditorViewType, text: string) {
  const range = view.state.selection.main;
  insertAtPosition(view, range.from, text, range.to);
}

function insertAtPosition(view: EditorViewType, from: number, text: string, to = from) {
  view.dispatch({
    changes: { from, to, insert: text },
    selection: { anchor: from + text.length },
    scrollIntoView: true,
  });
  view.focus();
}

export function insertBlockAttachmentAtRange(
  view: EditorViewType,
  requestedRange: { from: number; to: number },
  text: string,
  insertPrefix = "",
) {
  const range = safeBlockAttachmentRange(view, requestedRange);
  const documentText = view.state.doc.toString();
  const attachment = `${insertPrefix}${text}`.trim();
  const before = documentText.slice(0, range.from);
  const after = documentText.slice(range.to);
  const prefix = blockSeparatorBefore(before);
  const suffix = blockSeparatorAfter(after);
  const insert = `${prefix}${attachment}${suffix}`;
  view.dispatch({
    changes: { from: range.from, to: range.to, insert },
    selection: { anchor: range.from + insert.length },
    scrollIntoView: true,
  });
  view.focus();
}

function safeBlockAttachmentRange(
  view: EditorViewType,
  requestedRange: { from: number; to: number },
) {
  const documentLength = view.state.doc.length;
  const from = Math.max(0, Math.min(requestedRange.from, documentLength));
  const to = Math.max(from, Math.min(requestedRange.to, documentLength));
  if (from !== to) {
    return { from, to };
  }
  const fencedInsertion = insertionAfterContainingCodeFence(view, from);
  if (fencedInsertion !== null) {
    return { from: fencedInsertion, to: fencedInsertion };
  }
  const line = view.state.doc.lineAt(from);
  if (/^\s{0,3}#{1,6}(?:\s|$)/.test(line.text) ||
      /^\s*(?:[-+*]|\d+[.)])\s+/.test(line.text)) {
    return { from: line.to, to: line.to };
  }
  return { from, to };
}

function insertionAfterContainingCodeFence(
  view: EditorViewType,
  position: number,
) {
  let opening: { character: string; from: number; length: number } | null = null;
  for (let lineNumber = 1; lineNumber <= view.state.doc.lines; lineNumber += 1) {
    const line = view.state.doc.line(lineNumber);
    const match = /^\s{0,3}(`{3,}|~{3,})(.*)$/.exec(line.text);
    if (!match) {
      continue;
    }
    const marker = match[1];
    if (!opening) {
      opening = {
        character: marker[0],
        from: line.from,
        length: marker.length,
      };
      continue;
    }
    const closesFence = marker[0] === opening.character &&
      marker.length >= opening.length && match[2].trim().length === 0;
    if (!closesFence) {
      continue;
    }
    if (position >= opening.from && position <= line.to) {
      return line.to;
    }
    opening = null;
  }
  if (opening && position >= opening.from) {
    return opening.from;
  }
  return null;
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

export function insertImageFilesAtRange(
  view: EditorViewType,
  files: ArrayLike<File>,
  options: EditorPasteOptions = {},
  range: { from: number; to: number } = view.state.selection.main,
  _insertPrefix = "",
) {
  const imageSelection = selectImageUploads(files, options);
  if (imageSelection.placeholders.length === 0) {
    return imageSelection.handled;
  }
  addImageUploadSlotsAtRange(
    view,
    range,
    imageSelection.placeholders,
  );
  startImageUploads(
    view,
    imageSelection.placeholders,
    options,
    imageSelection.batchId,
  );
  return true;
}

export function retryImageUpload(
  view: EditorViewType,
  upload: ImageUploadPlaceholder,
  options: EditorPasteOptions,
) {
  if (upload.insertionSlotAvailable === false ||
      !hasEditorUploadSlot(view, upload.marker)) {
    return false;
  }
  const queue = editorUploadQueue(options);
  const retry = queue.retry(upload.marker);
  if (!retry) {
    const batchId = editorUploadBatchIdForMarker(options, upload.marker);
    startImageUploads(view, [upload], options, batchId);
  }
  return true;
}

export function canRetryImageUpload(options: EditorPasteOptions, marker: string) {
  const entry = editorUploadQueue(options).snapshot().find((item) => item.id === marker);
  return !entry || !isMediaPolicyChangedError(entry.error);
}

export function cancelImageUpload(options: EditorPasteOptions, marker: string) {
  const preparation = uploadPreparationMap(options).get(marker);
  if (preparation) {
    uploadPreparationMap(options).delete(marker);
    preparation.controller.abort();
    markUploadPreparationFinished(options, preparation.batchId, marker);
    notifyPreparationTerminal(preparation, "cancelled");
    return true;
  }
  return editorUploadQueue(options).cancel(marker);
}

export function cancelAllImageUploads(options: EditorPasteOptions) {
  for (const marker of [...uploadPreparationMap(options).keys()]) {
    cancelImageUpload(options, marker);
  }
  editorUploadQueue(options).cancelAll();
}

export function invalidateAllImageUploads(options: EditorPasteOptions) {
  const preparations = uploadPreparationMap(options);
  for (const preparation of preparations.values()) {
    preparation.silent = true;
    preparation.controller.abort();
  }
  preparations.clear();
  editorUploadQueue(options).clear();
  editorUploadBatchMap(options).clear();
  emitUploadBatches(options);
}

export function removeImageUpload(
  view: EditorViewType,
  upload: ImageUploadPlaceholder,
  options: EditorPasteOptions,
) {
  const removedSlot = removeEditorUploadSlot(view, upload.marker);
  const removedQueueEntry = editorUploadQueue(options).remove(upload.marker);
  const removedBatchItem = removeUploadBatchItem(options, upload.marker);
  return removedSlot || removedQueueEntry || removedBatchItem ||
    upload.insertionSlotAvailable === false;
}

function selectImageUploads(
  files: ArrayLike<File> | null | undefined,
  options: EditorPasteOptions,
) {
  const filesInOrder = Array.from(files ?? []);
  if (filesInOrder.length > 0 && options.isUploadCapabilityCurrent?.() === false) {
    const message = options.uploadCapabilitiesUnavailableMessage?.() ??
      "Current media upload limits are not available yet. Try again after they load.";
    for (const file of filesInOrder) {
      options.onUploadError?.(file, message);
    }
    return { batchId: null, handled: true, placeholders: [] };
  }
  const capabilities = options.getUploadBatchCapabilities?.();
  if (capabilities && filesInOrder.length > 0) {
    const candidates = filesInOrder.map((file) => ({
      file,
      id: uploadMarker(),
      validationError: options.validateImageFile?.(file) ?? null,
    }));
    const preflight = preflightMediaUploadBatch(candidates, capabilities);
    const batchId = registerEditorUploadBatch(options, preflight);
    for (const rejected of preflight.rejected) {
      options.onUploadError?.(
        rejected.file,
        rejected.rejectionMessage ?? "This file cannot be uploaded.",
        rejected.rejectionCode,
      );
    }
    return {
      batchId,
      handled: true,
      placeholders: preflight.accepted.map(({ file, id }, slotOrdinal) => {
        const name = sanitizeFileName(file.name || "image");
        return {
          file,
          marker: id,
          name,
          slotGroup: batchId,
          slotOrdinal,
        };
      }),
    };
  }
  const placeholders: ImageUploadPlaceholder[] = [];
  const slotGroup = uploadSlotGroup();
  let handled = false;
  for (const file of Array.from(files ?? [])) {
    const validationError = options.validateImageFile?.(file) ?? null;
    const candidate = isImageFile(file) ||
      (options.validateImageFile !== undefined && validationError === null);
    if (!candidate) {
      continue;
    }
    handled = true;
    if (validationError) {
      options.onUploadError?.(file, validationError);
      continue;
    }
    const marker = uploadMarker();
    const name = sanitizeFileName(file.name || "image");
    placeholders.push({
      file,
      marker,
      name,
      slotGroup,
      slotOrdinal: placeholders.length,
    });
  }
  return { batchId: null, handled, placeholders };
}

function startImageUploads(
  view: EditorViewType,
  placeholders: ImageUploadPlaceholder[],
  options: EditorPasteOptions,
  batchId: string | null,
) {
  const capabilities = options.getUploadBatchCapabilities?.();
  if (capabilities) {
    editorUploadQueue(options).setMaxConcurrency(
      resolveMediaUploadBatchLimits(capabilities).maxParallelUploads,
    );
  }
  const batch = batchId ? editorUploadBatchMap(options).get(batchId) : undefined;
  if (batch) {
    batch.view = view;
  }
  for (const placeholder of placeholders) {
    startEditorUploadPreparation(view, placeholder, options, batchId);
  }
}

function addImageUploadSlotsAtRange(
  view: EditorViewType,
  requestedRange: { from: number; to: number },
  uploads: readonly ImageUploadPlaceholder[],
) {
  const range = safeBlockAttachmentRange(view, requestedRange);
  addEditorUploadSlotsAtRange(
    view,
    range,
    uploads.map((upload) => ({
      group: upload.slotGroup,
      id: upload.marker,
      ordinal: upload.slotOrdinal,
    })),
  );
  view.focus();
}

function startEditorUploadPreparation(
  view: EditorViewType,
  upload: ImageUploadPlaceholder,
  options: EditorPasteOptions,
  batchId: string | null,
) {
  const controller = new AbortController();
  const preparation: EditorUploadPreparation = {
    batchId,
    controller,
    options,
    silent: false,
    terminalNotified: false,
    upload,
    view,
  };
  uploadPreparationMap(options).set(upload.marker, preparation);
  const batch = batchId ? editorUploadBatchMap(options).get(batchId) : undefined;
  batch?.preparingIds.add(upload.marker);
  options.onUploadStart?.(upload.file);
  notifyUploadState(options, upload, "queued", undefined, batchId);
  void prepareAndEnqueueEditorUpload(preparation);
}

async function prepareAndEnqueueEditorUpload(
  preparation: EditorUploadPreparation,
) {
  const { batchId, controller, options, upload, view } = preparation;
  try {
    const createSnapshot = options.createUploadTargetSnapshot;
    if (!createSnapshot) {
      throw new Error("Upload target is not available.");
    }
    const createdSnapshot = await createSnapshot(
      upload.file,
      upload.marker,
      controller.signal,
    );
    throwIfUploadAborted(controller.signal);
    const preparations = uploadPreparationMap(options);
    const snapshot = validatedUploadTargetSnapshot(createdSnapshot, upload);
    if (!isUploadTargetCurrent(options, snapshot)) {
      if (preparations.get(upload.marker) === preparation) {
        preparations.delete(upload.marker);
      }
      return;
    }
    if (preparations.get(upload.marker) !== preparation) {
      return;
    }
    preparations.delete(upload.marker);
    markUploadPreparationFinished(options, batchId, upload.marker);
    await editorUploadQueue(options).enqueue(upload.marker, {
      batchId,
      options,
      queuedAnnounced: true,
      snapshot,
      upload,
      view,
    });
  } catch (error) {
    const preparations = uploadPreparationMap(options);
    if (preparations.get(upload.marker) === preparation) {
      preparations.delete(upload.marker);
    }
    markUploadPreparationFinished(options, batchId, upload.marker);
    if (preparation.silent) {
      return;
    }
    const cancelled = controller.signal.aborted || isAbortLike(error);
    const message = cancelled ? undefined : uploadErrorMessage(error);
    if (!cancelled) {
      options.onUploadError?.(upload.file, message ?? "Upload failed.");
    }
    notifyPreparationTerminal(
      preparation,
      cancelled ? "cancelled" : "failed",
      message,
    );
  }
}

function uploadPreparationMap(options: EditorPasteOptions) {
  const existing = uploadPreparations.get(options);
  if (existing) return existing;
  const created = new Map<string, EditorUploadPreparation>();
  uploadPreparations.set(options, created);
  return created;
}

function notifyPreparationTerminal(
  preparation: EditorUploadPreparation,
  status: "failed" | "cancelled",
  message?: string,
) {
  if (preparation.silent || preparation.terminalNotified) return;
  preparation.terminalNotified = true;
  notifyUploadState(
    preparation.options,
    preparation.upload,
    status,
    message,
    preparation.batchId,
  );
  preparation.options.onUploadSettled?.(preparation.upload.file);
}

function validatedUploadTargetSnapshot(
  snapshot: UploadTargetSnapshot,
  upload: ImageUploadPlaceholder,
) {
  if (snapshot.assetOperationId !== upload.marker) {
    throw new Error("Upload target operation did not match its marker.");
  }
  validateUploadTargetMetadata(snapshot);
  const expectedFingerprint = payloadFingerprintForFile(upload.file);
  if (!samePayloadFingerprint(snapshot.payloadFingerprint, expectedFingerprint)) {
    throw new Error("Upload payload changed after target capture.");
  }
  return Object.freeze({
    ...snapshot,
    payloadFingerprint: Object.freeze({ ...snapshot.payloadFingerprint }),
  });
}

function validateUploadTargetMetadata(
  snapshot: Pick<
    UploadTargetSnapshot,
    | "actorSessionGeneration"
    | "editingVersionId"
    | "editorGeneration"
    | "pageId"
    | "policyRevision"
    | "workspaceId"
  >,
) {
  if (!snapshot.workspaceId.trim()) {
    throw new Error("Upload target workspace is not available.");
  }
  if (!snapshot.policyRevision.trim()) {
    throw new Error("Upload target policy revision is not available.");
  }
  if (!isUploadGeneration(snapshot.actorSessionGeneration) ||
      !isUploadGeneration(snapshot.editorGeneration)) {
    throw new Error("Upload target generation is invalid.");
  }
  if (!snapshot.pageId.trim()) {
    throw new Error("Upload target page is not available.");
  }
  if (!snapshot.editingVersionId.trim()) {
    throw new Error("Upload target editing version is not available.");
  }
}

function isUploadGeneration(value: number) {
  return Number.isSafeInteger(value) && value >= 0;
}

function isUploadTargetCurrent(
  options: EditorPasteOptions,
  snapshot: UploadTargetSnapshot,
) {
  return options.isUploadTargetCurrent?.(snapshot) ?? true;
}

function samePayloadFingerprint(
  left: UploadPayloadFingerprint,
  right: UploadPayloadFingerprint,
) {
  return left.fileName === right.fileName &&
    left.lastModified === right.lastModified &&
    left.mediaType === right.mediaType &&
    left.size === right.size;
}

function payloadFingerprintForFile(file: File): UploadPayloadFingerprint {
  return Object.freeze({
    fileName: normalizeFingerprintFileName(file.name || "image"),
    lastModified: Number.isFinite(file.lastModified) ? file.lastModified : 0,
    mediaType: file.type.trim().toLowerCase(),
    size: file.size,
  });
}

function throwIfUploadAborted(signal: AbortSignal) {
  if (signal.aborted) {
    throw staleUploadAbortError();
  }
}

function staleUploadAbortError() {
  return new DOMException("Upload target is no longer current.", "AbortError");
}

function isAbortLike(error: unknown) {
  return isUploadQueueAbortError(error);
}

function uploadErrorMessage(error: unknown) {
  return apiErrorMessage(error);
}

function registerEditorUploadBatch(
  options: EditorPasteOptions,
  preflight: MediaUploadPreflight,
) {
  const batchId = `editor-upload-batch-${++uploadBatchSequence}`;
  const placeholders = new Map<string, ImageUploadPlaceholder>();
  for (const [slotOrdinal, item] of preflight.accepted.entries()) {
    const name = sanitizeFileName(item.file.name || "image");
    placeholders.set(item.id, {
      file: item.file,
      marker: item.id,
      name,
      slotGroup: batchId,
      slotOrdinal,
    });
  }
  editorUploadBatchMap(options).set(batchId, {
    batchId,
    freshCapabilities: null,
    options,
    originalCandidates: preflight.items.map(({ file, id, validationError }) => ({
      file,
      id,
      validationError,
    })),
    placeholders,
    preparingIds: new Set(),
    refreshPromise: null,
    stalePolicyRevision: null,
    state: createMediaUploadCoordinatorState(batchId, preflight),
    view: null,
  });
  emitUploadBatches(options);
  return batchId;
}

function editorUploadBatchMap(options: EditorPasteOptions) {
  const existing = uploadBatches.get(options);
  if (existing) return existing;
  const created = new Map<string, EditorUploadBatch>();
  uploadBatches.set(options, created);
  return created;
}

function editorUploadBatchIdForMarker(
  options: EditorPasteOptions,
  marker: string,
) {
  for (const batch of editorUploadBatchMap(options).values()) {
    if (batch.state.items.some((item) => item.id === marker)) return batch.batchId;
  }
  return null;
}

function emitUploadBatches(options: EditorPasteOptions) {
  options.onUploadBatchesChange?.(
    Array.from(editorUploadBatchMap(options).values(), (batch) => batch.state),
  );
}

function notifyUploadState(
  options: EditorPasteOptions,
  upload: ImageUploadPlaceholder,
  status: ImageUploadLifecycleStatus,
  message: string | undefined,
  batchId: string | null,
) {
  const batch = batchId ? editorUploadBatchMap(options).get(batchId) : undefined;
  if (batch) {
    batch.state = reduceMediaUploadCoordinator(batch.state, {
      type: "item-status",
      id: upload.marker,
      status,
      message,
    });
    emitUploadBatches(options);
  }
  options.onUploadStateChange?.(upload, status, message);
}

function removeUploadBatchItem(options: EditorPasteOptions, marker: string) {
  const batches = editorUploadBatchMap(options);
  for (const [batchId, batch] of batches) {
    if (!batch.state.items.some((item) => item.id === marker)) continue;
    batch.state = reduceMediaUploadCoordinator(batch.state, {
      type: "remove-item",
      id: marker,
    });
    batch.placeholders.delete(marker);
    batch.originalCandidates = batch.originalCandidates.filter(
      (candidate) => candidate.id !== marker,
    );
    if (batch.state.items.length === 0) batches.delete(batchId);
    emitUploadBatches(options);
    return true;
  }
  return false;
}

function markUploadPreparationFinished(
  options: EditorPasteOptions,
  batchId: string | null,
  marker: string,
) {
  if (!batchId) return;
  const batch = editorUploadBatchMap(options).get(batchId);
  batch?.preparingIds.delete(marker);
  maybeCompletePolicyBatchRepreflight(options, batchId);
}

function schedulePolicyBatchRepreflight(task: EditorUploadTask) {
  const { batchId, options, snapshot, upload } = task;
  if (!batchId || !options.refreshUploadPolicy) return;
  const batch = editorUploadBatchMap(options).get(batchId);
  if (!batch) return;
  if (batch.state.policyRejectionMessage && !batch.state.policyRefreshRequired) return;
  batch.stalePolicyRevision ??= snapshot.policyRevision;
  batch.state = reduceMediaUploadCoordinator(batch.state, {
    type: "policy-refresh-required",
    id: upload.marker,
    stalePolicyRevision: snapshot.policyRevision,
  });
  emitUploadBatches(options);
  if (batch.refreshPromise || batch.freshCapabilities) return;
  batch.refreshPromise = options.refreshUploadPolicy(snapshot.policyRevision)
    .then((capabilities) => {
      batch.freshCapabilities = capabilities;
    })
    .catch((error: unknown) => {
      batch.state = reduceMediaUploadCoordinator(batch.state, {
        type: "policy-refresh-failed",
        message: uploadErrorMessage(error),
      });
      emitUploadBatches(options);
    })
    .finally(() => {
      batch.refreshPromise = null;
      maybeCompletePolicyBatchRepreflight(options, batchId);
    });
}

function maybeCompletePolicyBatchRepreflight(
  options: EditorPasteOptions,
  batchId: string | null,
) {
  if (!batchId) return;
  const batch = editorUploadBatchMap(options).get(batchId);
  if (!batch || !batch.freshCapabilities || batch.refreshPromise ||
      batch.preparingIds.size > 0 || !batch.stalePolicyRevision) return;
  const staleRevision = batch.stalePolicyRevision;
  const freshCapabilities = batch.freshCapabilities;
  const hasActiveStaleTask = editorUploadQueue(options).snapshot().some((entry) =>
    entry.value.batchId === batchId &&
    entry.value.snapshot.policyRevision === staleRevision &&
    (entry.status === "queued" || entry.status === "uploading" ||
      entry.status === "cancelling"));
  if (hasActiveStaleTask) return;

  let decision: ReturnType<typeof decideMediaUploadPolicyRepreflight>;
  try {
    const candidates = batch.originalCandidates.map(({ file, id }) => ({
      file,
      id,
      validationError: options.validateImageFile?.(file) ?? null,
    }));
    const preflight = preflightMediaUploadBatch(candidates, freshCapabilities);
    decision = decideMediaUploadPolicyRepreflight(batch.state, preflight);
  } catch (error) {
    batch.state = reduceMediaUploadCoordinator(batch.state, {
      type: "policy-refresh-failed",
      message: uploadErrorMessage(error),
    });
    batch.freshCapabilities = null;
    batch.stalePolicyRevision = null;
    emitUploadBatches(options);
    return;
  }

  batch.state = decision.state;
  batch.freshCapabilities = null;
  batch.stalePolicyRevision = null;
  const policyRejectionMessage = batch.state.policyRejectionMessage;
  if (policyRejectionMessage) {
    for (const marker of batch.state.stalePolicyItemIds) {
      const upload = batch.placeholders.get(marker);
      if (!upload) continue;
      batch.state = reduceMediaUploadCoordinator(batch.state, {
        type: "item-status",
        id: marker,
        status: "failed",
        message: policyRejectionMessage,
      });
      options.onUploadStateChange?.(
        upload,
        "failed",
        policyRejectionMessage,
      );
    }
    emitUploadBatches(options);
    return;
  }

  emitUploadBatches(options);
  const view = batch.view;
  if (!view) return;
  editorUploadQueue(options).setMaxConcurrency(
    resolveMediaUploadBatchLimits(freshCapabilities).maxParallelUploads,
  );
  for (const marker of decision.replayItemIds) {
    const upload = batch.placeholders.get(marker);
    if (!upload || upload.insertionSlotAvailable === false ||
        !hasEditorUploadSlot(view, marker) ||
        !editorUploadQueue(options).remove(marker)) continue;
    startEditorUploadPreparation(view, upload, options, batchId);
  }
}

function editorUploadQueue(options: EditorPasteOptions) {
  const existing = uploadQueues.get(options);
  if (existing) return existing;
  const queue = new UploadQueue<EditorUploadTask, true>({
    upload: runEditorUpload,
    errorMessage: uploadErrorMessage,
    retryDelayMs(error, attempt) {
      if (!(error instanceof ImageUploadApiError) || error.status !== 429) return null;
      return boundedMediaUploadRetryDelayMs(error.retryAfterMs, attempt);
    },
    onChange(entry) {
      const task = entry.value;
      const { batchId, options: taskOptions, snapshot, upload } = task;
      if (!isUploadTargetCurrent(taskOptions, snapshot)) return;
      if (entry.status === "queued" && task.queuedAnnounced) {
        task.queuedAnnounced = false;
        return;
      }
      if (entry.status === "failed" || entry.status === "cancelled" ||
          entry.status === "outcome-unknown") {
        if (entry.status === "failed") {
          taskOptions.onUploadError?.(upload.file, entry.message ?? "Upload failed.");
          if (isMediaPolicyChangedError(entry.error) && taskOptions.refreshUploadPolicy) {
            schedulePolicyBatchRepreflight(task);
          }
        }
      }
      notifyUploadState(taskOptions, upload, entry.status, entry.message, batchId);
      if (entry.status === "succeeded" || entry.status === "failed" ||
          entry.status === "cancelled" || entry.status === "outcome-unknown") {
        taskOptions.onUploadSettled?.(upload.file);
      }
      maybeCompletePolicyBatchRepreflight(taskOptions, batchId);
    },
  }, { activeCancelOutcomeUnknown: true });
  uploadQueues.set(options, queue);
  return queue;
}

async function runEditorUpload(task: EditorUploadTask, signal: AbortSignal) {
  const { options, snapshot, upload: placeholder, view } = task;
  const uploadImage = options.uploadImage ?? rejectMissingUploader;
  throwIfUploadAborted(signal);
  if (!isUploadTargetCurrent(options, snapshot)) {
    throw staleUploadAbortError();
  }
  const result = await uploadImage(placeholder.file, {
    editingVersionId: snapshot.editingVersionId,
    marker: placeholder.marker,
    pageId: snapshot.pageId,
    signal,
    targetSnapshot: snapshot,
    workspaceId: snapshot.workspaceId,
  });
  throwIfUploadAborted(signal);
  if (!isUploadTargetCurrent(options, snapshot)) {
    throw staleUploadAbortError();
  }
  if (!contextMediaAssetIdFromReference(result.logical_ref)) {
    throw new Error("Upload returned an invalid logical media reference.");
  }
  if (!resolveEditorUploadSlot(
    view,
    placeholder.marker,
    `![${escapeBracketText(placeholder.name)}](${result.logical_ref})`,
  )) {
    throw staleUploadAbortError();
  }
}

function invalidateRemovedUploadSlot(
  options: EditorPasteOptions,
  marker: string,
) {
  const preparation = uploadPreparationMap(options).get(marker);
  const entry = uploadQueues.get(options)?.snapshot().find((item) => item.id === marker);
  const upload = preparation?.upload ?? entry?.value.upload ??
    uploadFromBatches(options, marker);
  if (!upload) return;
  upload.insertionSlotAvailable = false;
  cancelImageUpload(options, marker);
  const finalEntry = uploadQueues.get(options)?.snapshot().find(
    (item) => item.id === marker,
  );
  const status = finalEntry?.status ?? (preparation ? "cancelled" : entry?.status);
  if (status) {
    notifyUploadState(
      options,
      upload,
      status,
      options.uploadInsertionInvalidatedMessage?.() ??
        "The upload insertion range changed. Remove this item and insert the image again.",
      finalEntry?.value.batchId ?? preparation?.batchId ?? entry?.value.batchId ?? null,
    );
  }
}

function uploadFromBatches(options: EditorPasteOptions, marker: string) {
  for (const batch of editorUploadBatchMap(options).values()) {
    const upload = batch.placeholders.get(marker);
    if (upload) return upload;
  }
  return undefined;
}

function rejectMissingUploader(): Promise<ImageUploadResult> {
  return Promise.reject(new Error("Upload endpoint is not available."));
}

function isImageFile(file: File) {
  return file.type.startsWith("image/");
}

function uploadMarker() {
  const browserCrypto = typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  if (browserCrypto?.randomUUID) {
    return browserCrypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (browserCrypto?.getRandomValues) {
    browserCrypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

function renderChildren(parent: ParentNode, context: RenderContext = {}) {
  return Array.from(parent.childNodes)
    .map((node) => renderNode(node, context))
    .join("");
}

type RenderContext = {
  inPre?: boolean;
  listDepth?: number;
};

function renderNode(node: ChildNode, context: RenderContext): string {
  if (node.nodeType === Node.TEXT_NODE) {
    return context.inPre ? node.textContent ?? "" : escapeMarkdownText(collapseText(node.textContent ?? ""));
  }
  if (!(node instanceof HTMLElement)) {
    return "";
  }

  const tag = node.tagName.toLowerCase();
  if (tag === "script" || tag === "style") {
    return "";
  }
  if (tag === "br") {
    return `${" ".repeat(2)}\n`;
  }
  if (tag === "hr") {
    return "\n\n---\n\n";
  }
  if (/^h[1-6]$/.test(tag)) {
    const level = Number(tag.slice(1));
    return block(`${"#".repeat(level)} ${inlineText(node)}`);
  }
  if (tag === "p" || tag === "div" || tag === "section" || tag === "article") {
    return block(renderChildren(node, context).trim());
  }
  if (tag === "strong" || tag === "b") {
    return wrapInline("**", renderChildren(node, context));
  }
  if (tag === "em" || tag === "i") {
    return wrapInline("_", renderChildren(node, context));
  }
  if (tag === "s" || tag === "del") {
    return wrapInline("~~", renderChildren(node, context));
  }
  if (tag === "code") {
    if (node.parentElement?.tagName.toLowerCase() === "pre") {
      return node.textContent ?? "";
    }
    return `\`${(node.textContent ?? "").replace(/`/g, "\\`")}\``;
  }
  if (tag === "pre") {
    const code = node.querySelector("code");
    const language = codeLanguage(code);
    return fencedCodeBlock(code?.textContent ?? node.textContent ?? "", language);
  }
  if (tag === "a") {
    return renderLink(node);
  }
  if (tag === "img") {
    return renderImage(node);
  }
  if (tag === "ul" || tag === "ol") {
    return renderList(node, tag === "ol", context);
  }
  if (tag === "blockquote") {
    return renderBlockquote(node, context);
  }
  if (tag === "table") {
    return renderTable(node);
  }
  return renderChildren(node, context);
}

function renderLink(node: HTMLElement) {
  const rawText = normalizeInline(node.textContent ?? "") || node.getAttribute("href") || "";
  const text = escapeBracketText(rawText);
  const internalSlug = node.getAttribute("data-internal-slug");
  if (internalSlug) {
    return `[[${sanitizeInternalSlug(internalSlug)}|${text}]]`;
  }
  const href = safeMarkdownDestination(node.getAttribute("href") ?? "", "link");
  if (!href) {
    return text;
  }
  return `[${text}](${href})`;
}

function renderImage(node: HTMLElement) {
  const alt = node.getAttribute("alt") ?? "image";
  const src = safeMarkdownDestination(node.getAttribute("src") ?? "", "image");
  if (!src) {
    return escapeMarkdownText(alt);
  }
  return `![${escapeBracketText(alt)}](${src})`;
}

function renderList(node: HTMLElement, ordered: boolean, context: RenderContext) {
  const depth = context.listDepth ?? 0;
  const items = Array.from(node.children).filter(
    (child): child is HTMLElement => child instanceof HTMLElement && child.tagName.toLowerCase() === "li",
  );
  const lines = items.map((item, index) => {
    const marker = ordered ? `${index + 1}. ` : "- ";
    const content = renderChildren(item, { ...context, listDepth: depth + 1 }).trim();
    const [firstLine, ...rest] = content.split("\n");
    const indent = "  ".repeat(depth);
    const continuation = rest.map((line) => `${indent}  ${line}`).join("\n");
    return continuation
      ? `${indent}${marker}${firstLine}\n${continuation}`
      : `${indent}${marker}${firstLine}`;
  });
  return `\n${lines.join("\n")}\n\n`;
}

function renderBlockquote(node: HTMLElement, context: RenderContext) {
  const content = renderChildren(node, context).trim();
  if (!content) {
    return "";
  }
  return `\n\n${content.split("\n").map((line) => `> ${line}`).join("\n")}\n\n`;
}

function renderTable(table: HTMLElement) {
  const rows = Array.from(table.querySelectorAll("tr")).map((row) =>
    Array.from(row.children)
      .filter((cell): cell is HTMLElement => cell instanceof HTMLElement)
      .map((cell) => inlineText(cell)),
  ).filter((row) => row.length > 0);
  if (rows.length === 0) {
    return "";
  }
  const columnCount = Math.max(...rows.map((row) => row.length));
  const normalizedRows = rows.map((row) => padRow(row, columnCount));
  const header = normalizedRows[0];
  const body = normalizedRows.slice(1);
  return `\n\n| ${header.join(" | ")} |\n| ${Array(columnCount).fill("---").join(" | ")} |\n${body.map((row) => `| ${row.join(" | ")} |`).join("\n")}\n\n`;
}

function padRow(row: string[], columnCount: number) {
  return [...row, ...Array(Math.max(0, columnCount - row.length)).fill("")];
}

function inlineText(node: ParentNode) {
  return normalizeInline(renderChildren(node).trim());
}

function block(content: string) {
  return content ? `\n\n${content}\n\n` : "";
}

function wrapInline(wrapper: string, content: string) {
  const trimmed = content.trim();
  return trimmed ? `${wrapper}${trimmed}${wrapper}` : "";
}

function codeLanguage(code: Element | null) {
  const className = code?.getAttribute("class") ?? "";
  const match = /(?:^|\s)language-([A-Za-z0-9_-]+)/.exec(className);
  return match ? match[1] : "";
}

function fencedCodeBlock(rawCode: string, language: string) {
  const code = rawCode.replace(/\n+$/g, "");
  const longestBacktickRun = Math.max(
    0,
    ...Array.from(code.matchAll(/`+/g), (match) => match[0].length),
  );
  const fence = "`".repeat(Math.max(3, longestBacktickRun + 1));
  return `\n\n${fence}${language}\n${code}\n${fence}\n\n`;
}

function collapseText(text: string) {
  return text.replace(/\s+/g, " ");
}

function normalizeInline(text: string) {
  return text.replace(/\s+/g, " ").trim();
}

function normalizeBlankLines(text: string) {
  return text
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function sanitizeFileName(name: string) {
  return name.replace(/[\\/\r\n\t]/g, "_").slice(0, 120);
}

function uploadSlotGroup() {
  return `editor-upload-group-${++uploadSlotGroupSequence}`;
}

function normalizeFingerprintFileName(name: string) {
  return sanitizeFileName(name.normalize("NFC")).trim();
}

function escapeBracketText(text: string) {
  return text.replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}

// Escape only CommonMark-significant punctuation. The previous version also
// escaped `@`, `^`, `<`, `>` which CommonMark does NOT treat as syntax — that
// turned a pasted email address ("alice@example.com") into "alice\@example.com"
// which round-trips as a visible backslash because remark won't un-escape `\@`.
// `<` and `>` are intentionally NOT escaped: bare auto-link syntax `<url>` is
// already gated by safeMarkdownDestination, and free-form `>` inside text
// becomes a blockquote only at the start of a line (caller controls insertion
// context). Review §3.1 FP-C5.
function escapeMarkdownText(text: string) {
  return text
    .replace(/\\/g, "\\\\")
    .replace(/`/g, "\\`")
    .replace(/\*/g, "\\*")
    .replace(/_/g, "\\_")
    .replace(/\[/g, "\\[")
    .replace(/\]/g, "\\]")
    .replace(/\(/g, "\\(")
    .replace(/\)/g, "\\)")
    .replace(/!/g, "\\!")
    .replace(/#/g, "\\#")
    .replace(/\{/g, "\\{")
    .replace(/\}/g, "\\}")
    .replace(/\|/g, "\\|");
}

function sanitizeInternalSlug(value: string) {
  return value
    .trim()
    .replace(/[\]\n\r|]/g, "")
    .slice(0, 120);
}

function safeMarkdownDestination(value: string, kind: "image" | "link") {
  const trimmed = value.trim();
  if (!trimmed || /[\u0000-\u001F\u007F]/.test(trimmed)) {
    return "";
  }
  if (kind === "image" && contextMediaAssetIdFromReference(trimmed)) {
    return trimmed;
  }
  const scheme = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(trimmed)?.[1].toLowerCase();
  if (scheme) {
    const allowed = kind === "image"
      ? new Set(["http", "https"])
      : new Set(["http", "https", "mailto"]);
    if (!allowed.has(scheme)) {
      return "";
    }
  }
  return trimmed
    .replace(/ /g, "%20")
    .replace(/\(/g, "%28")
    .replace(/\)/g, "%29")
    .replace(/</g, "%3C")
    .replace(/>/g, "%3E");
}
