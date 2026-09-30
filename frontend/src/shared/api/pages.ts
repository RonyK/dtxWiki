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

// P9.1: the backend's single canonical Context page shape (a superset of the
// old hand-rolled PagePayload -- see ContextPageDetail's docstring in the
// generated schema). Kept under the historical `PagePayload` name so callers
// migrated from the old /api/v1/pages surface do not all need renaming.
export type PageSummary = components["schemas"]["ContextPageSummary"];
export type PagePayload = components["schemas"]["ContextPageDetail"];

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

// P8.5: autosave targets the caller's own private "editing" row and requires
// a client-held idempotency key.
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

type PageResponse = components["schemas"]["ContextPageDetailResponse"];

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
  const response = await apiRequest<PageResponse>(
    pagesPath(workspaceId, `/${id}${query}`),
  );
  return response.data;
}

export async function discardAlternativeDraft(
  workspaceId: string,
  id: string,
  versionId: string,
  input: { reason: string },
) {
  const response = await apiRequest<{ data: PageVersionEntry }>(
    pagesPath(workspaceId, `/${id}/versions/${versionId}/discard`),
    { method: "POST", body: JSON.stringify(input) },
  );
  return response.data;
}
export async function diffPageVersions(
  workspaceId: string,
  id: string,
  fromVersionId: string | null,
  toVersionId: string,
  options: { signal?: AbortSignal } = {},
) {
  const query = new URLSearchParams();
  if (fromVersionId === null) {
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

export type PageWriteOptions = {
  signal?: AbortSignal;
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

export async function publishPageVersion(
  workspaceId: string,
  id: string,
  versionId: string,
  payload: PublishPageVersionInput,
  options: PageWriteOptions = {},
) {
  const response = await apiRequest<PageResponse>(
    pagesPath(workspaceId, `/${id}/versions/${versionId}/publish`),
    {
      method: "POST",
      body: JSON.stringify(payload),
      signal: options.signal,
    },
  );
  return {
    page: response.data,
    warnings: response.envelope?.warnings ?? [],
  };
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
    const response = await apiRequest<PageResponse>(
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
