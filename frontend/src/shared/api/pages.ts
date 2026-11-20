import { ApiError, apiRequest, getApiBaseUrl, getApiToken } from "./client";
import type { components, paths } from "./types.gen";

// P8.5: one idempotency key per autosave attempt, generated before the first
// send (a retry reuses the same key; a new edit generates a new one). Falls
// back to getRandomValues the same way editorPaste.ts's upload marker does,
// for browsers/test environments without crypto.randomUUID.
export function newIdempotencyKey(): string {
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
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Editor state uses the fields shared by reads and write responses. Draft
// discovery metadata belongs to the richer read response only.
export type PageSummary = components["schemas"]["ContextPageSummary"];
export type PagePayload = components["schemas"]["ContextPageWriteDetail"];

export type PageVersionEntry = components["schemas"]["PageVersionEntry"];

export type PageVersionDiff = components["schemas"]["PageVersionDiff"];

export type PageInlineCommentAnchor = {
  anchor_text: string;
  context_before: string;
  context_after: string;
  version_id: string;
  char_offset_hint: number;
};

export type PageInlineCommentResolved = {
  char_offset: number | null;
  end_offset: number | null;
  status: "exact" | "fuzzy" | "fuzzy_distant" | "orphan";
  resolved_text: string | null;
};

export type PageInlineComment = {
  id: string;
  page_id: string;
  review_item_id: string;
  change_set_id: string;
  change_operation_id: string;
  status: string;
  risk_level: string;
  note: string;
  anchor: PageInlineCommentAnchor;
  resolved: PageInlineCommentResolved;
  created_at: string;
  updated_at: string;
};

export type PageInlineCommentCreateInput = {
  note: string;
  anchor: PageInlineCommentAnchor;
};

export type PageCreateInput = components["schemas"]["ContextPageCreateRequest"];

export type PageUpdateInput = components["schemas"]["PageUpdateRequest"];

// Browser saves stage shared drafts and require a client-held retry key.
export type PageAutosaveInput = components["schemas"]["PageAutosaveRequest"];

export type PageRestoreInput = components["schemas"]["PageRestoreRequest"];

export type PublishPageVersionInput =
  components["schemas"]["PublishPageVersionRequest"];

type PageListResponse = {
  data: PageSummary[];
  next_cursor: string | null;
};

type PageTagListResponse = {
  data: string[];
};

type PageReadResponse = components["schemas"]["ContextPageDetailResponse"];
type PageResponse = components["schemas"]["ContextPageWriteResponse"];

type PageVersionDiffResponse = {
  data: PageVersionDiff;
};

type PageInlineCommentResponse = {
  data: PageInlineComment;
};

export type OpenApiPaths = paths;

function pagesPath(workspaceId: string, suffix = "") {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/pages${suffix}`;
}

// The backend caps each list response at 100 rows (default 50), so a single
// request silently truncates larger workspaces. Follow next_cursor until the
// list is exhausted, with a hard request cap as a runaway guard (10 * 100 =
// 1,000 pages). next_cursor is only non-null when the cap was hit.
const PAGE_LIST_REQUEST_LIMIT = 100;
const PAGE_LIST_MAX_REQUESTS = 10;

export async function listPages(workspaceId: string) {
  const data: PageSummary[] = [];
  let cursor: string | null = null;
  for (let request = 0; request < PAGE_LIST_MAX_REQUESTS; request += 1) {
    const response = await listPagesPage(workspaceId, { cursor });
    data.push(...response.data);
    cursor = response.next_cursor;
    if (!cursor) {
      break;
    }
  }
  return { data, next_cursor: cursor } satisfies PageListResponse;
}

// Single-page fetch, following no cursor of its own -- callers that need
// unbounded reach (e.g. a Load-more-driven list) page through this directly
// instead of going through listPages's 1,000-row cap.
export async function listPagesPage(
  workspaceId: string,
  options: { cursor?: string | null; limit?: number } = {},
) {
  const query = new URLSearchParams({
    limit: String(options.limit ?? PAGE_LIST_REQUEST_LIMIT),
  });
  if (options.cursor) {
    query.set("cursor", options.cursor);
  }
  return apiRequest<PageListResponse>(
    pagesPath(workspaceId, `?${query.toString()}`),
  );
}

export async function listPageTags(workspaceId: string, q = "", limit = 20) {
  const query = new URLSearchParams({
    limit: String(limit),
  });
  if (q.trim()) {
    query.set("q", q.trim());
  }
  const response = await apiRequest<PageTagListResponse>(
    pagesPath(workspaceId, `/tags?${query.toString()}`),
  );
  return response.data;
}

export async function readPage(
  workspaceId: string,
  id: string,
  versionId?: string,
) {
  const query = versionId
    ? `?${new URLSearchParams({ version_id: versionId }).toString()}`
    : "";
  const response = await apiRequest<PageReadResponse>(
    pagesPath(workspaceId, `/${id}${query}`),
  );
  return response.data;
}

// A successful read can expose a draft that this actor cannot resume editing.
// Keep that state separate from an HTTP not-found or failed read.
export class PageEditingUnavailableError extends Error {
  constructor() {
    super("No editable version is available for this actor");
    this.name = "PageEditingUnavailableError";
  }
}

// Published versions are read-only entry points until the first save starts a
// draft. Explicit shared drafts and never-published pages join the existing
// draft; an ended draft named from history starts a new one when opened.
export async function readEditablePage(workspaceId: string, id: string, versionId?: string) {
  const selected = await readPage(workspaceId, id, versionId);
  const joinsDraft = selected.version_status === "draft" && (versionId || !selected.current_published_version_id);
  const endedDraft = Boolean(versionId) &&
    (selected.version_status === "closed" || selected.version_status === "discarded");
  return joinsDraft || endedDraft ? beginPageEdit(workspaceId, selected.id, selected.version_id) : selected;
}

/** Joins a named draft, or starts a new draft from a finished commit (or the published version). */
export async function beginPageEdit(workspaceId: string, pageId: string, fromVersionId?: string) {
  const response = await apiRequest<PageResponse>(pagesPath(workspaceId, `/${pageId}/begin-edit`), {
    method: "POST",
    body: JSON.stringify({ idempotency_key: newIdempotencyKey(), ...(fromVersionId ? { from: fromVersionId } : {}) }),
  });
  return response.data;
}

export async function submitPageForReview(
  workspaceId: string,
  pageId: string,
  payload: components["schemas"]["SubmitContextEditRequest"],
) {
  const response = await apiRequest<PageResponse>(pagesPath(workspaceId, `/${pageId}/submit-for-review`), {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return response.data;
}

export async function discardPageEdit(workspaceId: string, id: string, versionId: string) {
  await apiRequest<void>(pagesPath(workspaceId, `/${id}/discard-edit`), {
    method: "POST",
    body: JSON.stringify({ version_id: versionId, idempotency_key: newIdempotencyKey(), retry_only: false }),
  });
}

export async function diffPageVersions(
  workspaceId: string,
  id: string,
  fromVersionId: string | null,
  toVersionId: string,
  options: { signal?: AbortSignal; publishResult?: boolean } = {},
) {
  const query = new URLSearchParams();
  if (options.publishResult) {
    // The current published version -> what publishing `toVersionId` would
    // publish (the merge, for a draft that is behind).
    query.set("baseline", "publish_result");
  } else if (fromVersionId === null) {
    query.set("baseline", "empty");
  } else {
    query.set("from_version_id", fromVersionId);
  }
  query.set("to_version_id", toVersionId);
  const response = await apiRequest<PageVersionDiffResponse>(
    pagesPath(workspaceId, `/${id}/diff?${query.toString()}`),
    { signal: options.signal },
  );
  return response.data;
}

export async function restorePageVersion(
  workspaceId: string,
  id: string,
  versionId: string,
) {
  const response = await apiRequest<PageResponse>(
    pagesPath(workspaceId, `/${id}/restore`),
    {
      method: "POST",
      body: JSON.stringify({ version_id: versionId } satisfies PageRestoreInput),
    },
  );
  return response.data;
}

export async function createPageComment(
  workspaceId: string,
  id: string,
  payload: PageInlineCommentCreateInput,
) {
  const response = await apiRequest<PageInlineCommentResponse>(
    pagesPath(workspaceId, `/${id}/comments`),
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}

export type PreparedPagePublication = { versionId: string; request: PublishPageVersionInput };
export type PageWriteOptions = {
  signal?: AbortSignal;
  /** The merge result a publication writes, bound to its preview. */
  mergeChoice?: PreparedMergeChoice;
  preparedPublication?: PreparedPagePublication;
  onPublicationPrepared?: (command: PreparedPagePublication) => void;
};

export async function createPage(
  workspaceId: string,
  payload: PageCreateInput,
  options: PageWriteOptions = {},
) {
  const response = await apiRequest<PageResponse>(pagesPath(workspaceId), {
    method: "POST",
    body: JSON.stringify(payload),
    signal: options.signal,
  });
  return response.data;
}

export async function updatePage(
  workspaceId: string,
  id: string,
  payload: PageUpdateInput,
  options: PageWriteOptions = {},
) {
  const response = await apiRequest<PageResponse>(
    pagesPath(workspaceId, `/${id}`),
    {
      method: "PATCH",
      body: JSON.stringify(payload),
      signal: options.signal,
    },
  );
  return response.data;
}

export async function autosavePage(
  workspaceId: string,
  id: string,
  payload: PageAutosaveInput,
  options: PageWriteOptions = {},
) {
  const response = await apiRequest<PageResponse>(
    pagesPath(workspaceId, `/${id}/autosave`),
    {
      method: "POST",
      body: JSON.stringify(payload),
      signal: options.signal,
    },
  );
  return response.data;
}

export type ContextMergePreview = components["schemas"]["ContextMergePreviewData"];
export type ContextMergeCandidate = components["schemas"]["ContextMergeCandidate"];
export type ContextMergeChoiceValue = "keep_both" | "latest";
export type ContextMergeMetadataInput = components["schemas"]["PageMetadataPatch"];

export type DraftBaseSnapshot = components["schemas"]["DraftBaseSnapshot"];

/** The saved draft state a later autosave merges against. */
export function draftBaseSnapshotOf(page: Pick<PagePayload, "body_md" | "page_metadata">): DraftBaseSnapshot {
  return { body_md: page.body_md, page_metadata: page.page_metadata ?? {} };
}

/** An autosave that overlapped another save of the draft: the server draft and both choices. */
export type ContextMergeConflict = components["schemas"]["ContextMergeConflictBody"];

/** The typed merge-conflict body of a failed autosave; null for any other error. */
export function contextMergeConflictOf(error: unknown): ContextMergeConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409 || error.body.error_code !== "context_merge_conflict") return null;
  const body = error.body as Partial<ContextMergeConflict>;
  return body.merge_preview && body.server_snapshot && body.server_editing_revision != null
    ? body as ContextMergeConflict
    : null;
}

/** The preview changed between what the person saw and the confirm. */
export class ContextMergePreviewChanged extends Error {
  constructor() {
    super("The content changed; try again with the new preview.");
    this.name = "ContextMergePreviewChanged";
  }
}

/**
 * A publish, approval or bring-in refused because what it was bound to moved:
 * the draft was saved, the published version or the merge changed, or the
 * draft's open review changed. The caller shows the new state; other refusals
 * keep their own message.
 */
const CONTEXT_STATE_MOVED = new Set([
  "context_merge_conflict",
  "context_review_changed",
  "editing_revision_conflict",
  "stale_base_version",
  "stale_context_draft",
]);
export function contextWriteStateMoved(error: unknown) {
  return error instanceof ContextMergePreviewChanged
    || (error instanceof ApiError && error.status === 409 && CONTEXT_STATE_MOVED.has(error.body.error_code));
}

/** The preview facts a merge write is bound to. */
type MergePreviewPins = Pick<ContextMergePreview, "editing_revision" | "target_version_id" | "content_hash" | "references_hash">;

/**
 * A merge result bound to the preview it came from. `chosen` when a person
 * picked it in the merge dialog (and so saw the preview's draft); `clean` when
 * the merge needed no choice.
 */
export type PreparedMergeChoice = {
  source: "clean" | "chosen";
  preview: MergePreviewPins;
  result_hash: string;
  on_conflict?: ContextMergeChoiceValue;
  result_body_md?: string;
  result_page_metadata?: ContextMergeMetadataInput;
};

function previewPins(preview: ContextMergePreview): MergePreviewPins {
  return {
    editing_revision: preview.editing_revision,
    target_version_id: preview.target_version_id,
    content_hash: preview.content_hash,
    references_hash: preview.references_hash,
  };
}

export async function previewPageMerge(workspaceId: string, id: string, versionId: string,
  options: {
    signal?: AbortSignal;
    commitVersionId?: string | null;
    onConflict?: ContextMergeChoiceValue;
    bodyMd?: string;
    pageMetadata?: ContextMergeMetadataInput;
  } = {}) {
  const response = await apiRequest<{ data: ContextMergePreview }>(pagesPath(workspaceId, `/${id}/merge-preview`), {
    method: "POST",
    body: JSON.stringify({
      version_id: versionId,
      ...(options.commitVersionId ? { commit_version_id: options.commitVersionId } : {}),
      ...(options.onConflict ? { on_conflict: options.onConflict } : {}),
      ...(options.bodyMd !== undefined ? { body_md: options.bodyMd } : {}),
      ...(options.pageMetadata ? { page_metadata: options.pageMetadata } : {}),
    }),
    signal: options.signal,
  });
  return response.data;
}

/** The merge needs no choice: what the server previewed is what it writes. */
export function cleanMergeChoice(preview: ContextMergePreview): PreparedMergeChoice {
  if (!preview.result) throw new Error("A conflicting merge preview has no clean result.");
  return { source: "clean", preview: previewPins(preview), result_hash: preview.result.result_hash };
}

/**
 * Binds a person's choice (and optional edits) to the preview they saw. An
 * edited result is hashed by the server; if the draft or the other side moved
 * meanwhile, it is refused with an error instead of overwriting the change.
 */
export async function prepareMergeChoice(workspaceId: string, id: string, preview: ContextMergePreview,
  choice: { onConflict: ContextMergeChoiceValue; bodyMd?: string | null; pageMetadata?: ContextMergeMetadataInput | null },
  options: { signal?: AbortSignal } = {}): Promise<PreparedMergeChoice> {
  const edits = {
    ...(choice.bodyMd != null ? { bodyMd: choice.bodyMd } : {}),
    ...(choice.pageMetadata != null ? { pageMetadata: choice.pageMetadata } : {}),
  };
  let resultHash = preview[choice.onConflict]?.result_hash;
  if (Object.keys(edits).length > 0 || !resultHash) {
    const target = { signal: options.signal, commitVersionId: preview.target_version_id };
    const checked = await previewPageMerge(workspaceId, id, preview.version_id, { ...target, onConflict: choice.onConflict, ...edits });
    if (checked.editing_revision !== preview.editing_revision || checked.target_version_id !== preview.target_version_id || !checked.result) {
      throw new ContextMergePreviewChanged();
    }
    resultHash = checked.result.result_hash;
  }
  return {
    source: "chosen",
    preview: previewPins(preview),
    result_hash: resultHash,
    on_conflict: choice.onConflict,
    ...(choice.bodyMd != null ? { result_body_md: choice.bodyMd } : {}),
    ...(choice.pageMetadata != null ? { result_page_metadata: choice.pageMetadata } : {}),
  };
}

/** The fields every merge write sends to bind its result to the preview. */
export function mergeResultPins(choice: PreparedMergeChoice) {
  return {
    expected_editing_revision: choice.preview.editing_revision,
    result_hash: choice.result_hash,
    ...(choice.on_conflict ? { on_conflict: choice.on_conflict } : {}),
    ...(choice.result_body_md !== undefined ? { result_body_md: choice.result_body_md } : {}),
    ...(choice.result_page_metadata !== undefined ? { result_page_metadata: choice.result_page_metadata } : {}),
  };
}

type PublicationSource = Pick<PublishPageVersionInput, "expected_content_hash" | "expected_references_hash">;

/**
 * Publication pins. The draft being published is the one the person saw: the
 * preview's draft after a choice in the dialog, otherwise the draft they saved.
 */
export function publicationPins(choice: PreparedMergeChoice, saved: PublicationSource) {
  const seen = choice.source === "chosen"
    ? { expected_content_hash: choice.preview.content_hash, expected_references_hash: choice.preview.references_hash }
    : saved;
  return { ...mergeResultPins(choice), expected_published_version_id: choice.preview.target_version_id, ...seen };
}

/** Bring-in pins: the commit the preview merged is the commit brought in. */
export function bringInPins(choice: PreparedMergeChoice) {
  if (!choice.preview.target_version_id) throw new Error("A bring-in preview names the commit it merges.");
  return { ...mergeResultPins(choice), commit_version_id: choice.preview.target_version_id };
}

export async function publishPageVersion(
  workspaceId: string,
  id: string,
  versionId: string,
  payload: Pick<PublishPageVersionInput, "reason" | "publish_despite_findings" | "expected_content_hash" | "expected_references_hash" | "expected_review_item_id">,
  options: PageWriteOptions = {},
) {
  let command = options.preparedPublication;
  if (!command) {
    if (!options.mergeChoice) throw new Error("Publication needs a merge choice or a prepared command.");
    const {
      reason,
      publish_despite_findings: publishDespiteFindings,
      expected_review_item_id: expectedReviewItemId,
      ...saved
    } = payload;
    command = { versionId, request: {
      reason,
      publish_despite_findings: publishDespiteFindings,
      // The review the person saw (null: none). A publish approves the
      // draft's open review, so a different one refuses the write.
      ...(expectedReviewItemId !== undefined ? { expected_review_item_id: expectedReviewItemId } : {}),
      ...publicationPins(options.mergeChoice, saved),
      idempotency_key: newIdempotencyKey(),
    } };
    options.onPublicationPrepared?.(command);
  }
  return executePreparedPagePublication(workspaceId, id, command, options);
}

/** Brings the commit a merge preview named into a draft. */
export async function bringInPageVersion(workspaceId: string, id: string, versionId: string,
  choice: PreparedMergeChoice, options: { signal?: AbortSignal } = {}) {
  const response = await apiRequest<PageResponse>(pagesPath(workspaceId, `/${id}/bring-in`), {
    method: "POST",
    body: JSON.stringify({ version_id: versionId, ...bringInPins(choice), idempotency_key: newIdempotencyKey() }),
    signal: options.signal,
  });
  return response.data;
}

export async function executePreparedPagePublication(workspaceId: string, id: string,
  command: PreparedPagePublication, options: PageWriteOptions = {}) {
  const response = await apiRequest<PageResponse>(
    pagesPath(workspaceId, `/${id}/versions/${command.versionId}/publish`),
    { method: "POST", body: JSON.stringify(command.request), signal: options.signal },
  );
  return { page: response.data, warnings: response.envelope?.warnings ?? [] };
}

// Conservative cap for sendBeacon / keepalive fetch bodies. Chromium enforces
// a ~64 KiB per-origin quota on both transports and blocks synchronous XHR
// inside page-dismissal handlers, so no transport reliably delivers a larger
// autosave during unload. Oversized payloads are still attempted best-effort
// (browsers without the quota may accept them), but sendPageAutosave returns
// false so callers know delivery is not assured; the editor's debounced
// autosave bounds the possible loss to the last edit window. Review C2.10.
export const PAGE_AUTOSAVE_BEACON_LIMIT_BYTES = 60_000;

function utf8ByteLength(value: string): number {
  if (typeof TextEncoder !== "undefined") {
    return new TextEncoder().encode(value).byteLength;
  }
  // Fallback (older environments): conservative upper bound.
  return value.length * 4;
}

export function sendPageAutosave(
  workspaceId: string,
  id: string,
  payload: PageAutosaveInput,
) {
  const body = JSON.stringify(payload);
  const apiToken = getApiToken();
  const url = `${getApiBaseUrl()}${pagesPath(workspaceId, `/${id}/autosave`)}`;
  const bodyBytes = utf8ByteLength(body);
  const withinBeaconLimit = bodyBytes <= PAGE_AUTOSAVE_BEACON_LIMIT_BYTES;

  if (!withinBeaconLimit && typeof console !== "undefined") {
    console.warn(
      `sendPageAutosave: payload ${bodyBytes}B exceeds the ${PAGE_AUTOSAVE_BEACON_LIMIT_BYTES}B ` +
        "beacon/keepalive budget; the unload autosave may be dropped.",
    );
  }

  // sendBeacon survives unload and reports quota rejection synchronously, but
  // it cannot carry an Authorization header, so it only serves the cookie-auth
  // path. When it declines the payload, fall through to keepalive fetch.
  if (!apiToken && typeof navigator !== "undefined" && navigator.sendBeacon) {
    const queued = navigator.sendBeacon(
      url,
      new Blob([body], { type: "application/json" }),
    );
    if (queued) {
      return true;
    }
  }

  const headers = new Headers({
    Accept: "application/json",
    "Content-Type": "application/json",
  });
  if (apiToken) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }
  try {
    void fetch(url, {
      method: "POST",
      body,
      credentials: "include",
      headers,
      keepalive: true,
    }).catch(() => {
      // Best-effort during unload: a rejection (e.g. Chromium's keepalive
      // body quota) can no longer be reported to the caller here.
    });
  } catch {
    return false;
  }
  return withinBeaconLimit;
}

export async function readPageBySlug(workspaceId: string, slug: string) {
  try {
    const response = await apiRequest<PageReadResponse>(
      pagesPath(workspaceId, `/by-slug/${encodeURIComponent(slug)}`),
    );
    return response.data;
  } catch (exc) {
    if (exc instanceof ApiError && exc.status === 404) {
      return null;
    }
    throw exc;
  }
}
