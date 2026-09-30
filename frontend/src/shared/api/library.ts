import {
  ApiError,
  type ApiErrorBody,
  apiRequest,
  getApiBaseUrl,
  getApiToken,
} from "./client";
import {
  normalizeLibraryCapabilities,
  type LibraryCapabilities,
  type LibraryFormatCapability,
} from "../media/libraryFormatCapabilities";

export type { LibraryCapabilities, LibraryFormatCapability };

const POTENTIALLY_ACTIVE_ORIGINAL_FORMATS = new Set([
  "docx",
  "html",
  "javascript",
  "pptx",
  "xlsx",
]);

export function libraryOriginalRequiresWarning(
  formatId: string | null | undefined,
) {
  return typeof formatId === "string" && POTENTIALLY_ACTIVE_ORIGINAL_FORMATS.has(
    formatId.trim().toLowerCase(),
  );
}

export type LibraryItemSummary = {
  id: string;
  uri: string;
  title: string | null;
  connector_type: string;
  format_hint: string;
  trust_level: string;
  lifecycle_status: string;
  current_version_id: string | null;
  current_ready_version_id?: string | null;
  current_preview_version_id?: string | null;
  preview_status?: string | null;
  processing_status?: string | null;
  original_url?: string | null;
  preview_url?: string | null;
  processing?: string | { status?: string | null } | null;
  updated_at: string;
};

export type LibraryItemVersionPayload = {
  id: string;
  library_item_id: string;
  version_no: number;
  content_hash: string;
  format_id?: string | null;
  raw_storage_ref?: string | null;
  selected_conversion_attempt_id?: string | null;
  original_url?: string | null;
  // Doc §3-D: server-authoritative -- only true once the original blob was
  // actually verified retrievable, not merely that a blob id is set. History
  // uses this (not `original_url` presence) to decide whether to offer a
  // per-version download action.
  original_download_available?: boolean;
  preview_url?: string | null;
  processing_status: string;
  conversion?: {
    status: string;
    reason:
      | "document_conversion_failed"
      | "document_conversion_timed_out"
      | "document_conversion_unavailable"
      | null;
    retryable: boolean;
  };
  preview_status?: string | null;
  extraction_summary: string | null;
  previous_version_id: string | null;
  created_at: string;
  fetched_at: string;
};

export type LibraryArtifactPayload = {
  id: string;
  library_item_version_id: string;
  artifact_type: string;
  role?: string | null;
  media_type?: string | null;
  byte_size?: number | null;
  ordinal?: number | null;
  is_complete?: boolean;
  storage_ref?: string | null;
  preview_url?: string | null;
  content_url?: string | null;
  extraction_method: string | null;
  index_text: string | null;
  location_model: Record<string, unknown>;
  quality_score: number | null;
  created_at: string;
};

export type LibraryItemDetail = LibraryItemSummary & {
  metadata: Record<string, unknown>;
  current_version: LibraryItemVersionPayload | null;
  current_ready_version?: LibraryItemVersionPayload | null;
  versions: LibraryItemVersionPayload[];
  artifacts: LibraryArtifactPayload[];
};

export type LibraryOriginalDownload = {
  blob: Blob;
  filename: string | null;
};

export class LibraryUploadApiError extends ApiError {
  readonly retryAfterSeconds: number | null;
  readonly retryable: boolean;

  constructor(
    status: number,
    body: ApiErrorBody,
    retryAfterSeconds: number | null,
  ) {
    super(status, body);
    this.name = "LibraryUploadApiError";
    this.retryAfterSeconds = retryAfterSeconds;
    this.retryable = libraryUploadErrorIsRetryable(status, body.error_code);
  }
}

const PROTECTED_LIBRARY_ORIGINAL_PATH =
  /^\/api\/v1\/workspaces\/[^/?#]+\/library\/versions\/[^/?#]+\/content$/;

export const LEGACY_LIBRARY_CAPABILITIES: LibraryCapabilities = {
  policy_revision: "legacy",
  capability_revision: "legacy",
  max_files_per_batch: 20,
  formats: [
    legacyFormat("markdown", [".md", ".markdown"], ["text/markdown"]),
    legacyFormat("text", [".txt"], ["text/plain"]),
    legacyFormat("csv", [".csv"], ["text/csv"]),
    legacyFormat("json", [".json"], ["application/json"]),
    legacyFormat("yaml", [".yaml", ".yml"], ["application/yaml", "text/yaml"]),
  ],
};

export const UNAVAILABLE_LIBRARY_CAPABILITIES: LibraryCapabilities = {
  policy_revision: "unavailable",
  capability_revision: "unavailable",
  max_files_per_batch: 1,
  formats: [],
};

export async function getLibraryCapabilities(
  workspaceId: string,
): Promise<LibraryCapabilities> {
  const response = await apiRequest<{ data: unknown }>(
    `/api/v1/workspaces/${workspaceId}/library/capabilities`,
  );
  return normalizeLibraryCapabilities(
    response.data,
    UNAVAILABLE_LIBRARY_CAPABILITIES,
  );
}

export function libraryUploadAccept(capabilities: LibraryCapabilities) {
  const accepted = new Set<string>();
  for (const format of capabilities.formats) {
    if (!format.upload) continue;
    for (const extension of format.extensions) {
      const normalized = normalizeExtension(extension);
      if (normalized) accepted.add(normalized);
    }
    for (const mediaType of format.media_types) {
      const normalized = mediaType.trim().toLowerCase();
      if (normalized) accepted.add(normalized);
    }
  }
  return Array.from(accepted).join(",");
}

export function matchingLibraryFormat(
  file: Pick<File, "name" | "type">,
  capabilities: LibraryCapabilities,
) {
  const filename = file.name.toLowerCase();
  const mediaType = file.type.trim().toLowerCase();
  const uploadable = capabilities.formats.filter((format) => format.upload);
  return uploadable.find((format) =>
    format.extensions.some((extension) => {
      const normalized = normalizeExtension(extension);
      return normalized !== "" && filename.endsWith(normalized);
    }),
  ) ?? uploadable.find((format) =>
    mediaType !== "" && format.media_types.some((candidate) => candidate.toLowerCase() === mediaType),
  ) ?? null;
}

export type LibraryItemListOptions = {
  connector?: string;
  lifecycle?: string;
  limit?: number;
  trust?: string;
  cursor?: string | null;
};

export type LibraryItemListResult = {
  data: LibraryItemSummary[];
  next_cursor: string | null;
  total: number | null;
  has_more: boolean;
};

export async function listLibraryItems(
  workspaceId: string,
  options: LibraryItemListOptions = {},
): Promise<LibraryItemListResult> {
  const query = new URLSearchParams();
  if (options.connector) query.set("connector", options.connector);
  if (options.lifecycle) query.set("lifecycle", options.lifecycle);
  if (options.limit !== undefined) query.set("limit", String(normalizeLibraryLimit(options.limit)));
  if (options.trust) query.set("trust", options.trust);
  if (options.cursor) query.set("cursor", options.cursor);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const response = await apiRequest<{
    data: LibraryItemSummary[];
    next_cursor: string | null;
    total: number | null;
    has_more: boolean;
  }>(`/api/v1/workspaces/${workspaceId}/library/items${suffix}`);
  return {
    data: response.data,
    next_cursor: response.next_cursor ?? null,
    total: response.total ?? null,
    has_more: response.has_more ?? false,
  };
}

function normalizeLibraryLimit(limit: number) {
  if (!Number.isFinite(limit)) {
    return 100;
  }
  return Math.min(100, Math.max(1, Math.trunc(limit)));
}

export async function readLibraryItem(workspaceId: string, libraryItemId: string) {
  const response = await apiRequest<{ data: LibraryItemDetail }>(
    `/api/v1/workspaces/${workspaceId}/library/items/${libraryItemId}`,
  );
  return response.data;
}

export async function retryLibraryConversion(
  workspaceId: string,
  versionId: string,
) {
  const response = await apiRequest<{ data: LibraryItemDetail }>(
    `/api/v1/workspaces/${workspaceId}/library/versions/${versionId}/conversion/retry`,
    { method: "POST" },
  );
  return response.data;
}

export type LibraryItemReference = {
  id: string;
  title: string | null;
  slug: string;
};

export async function listLibraryItemReferences(
  workspaceId: string,
  libraryItemId: string,
) {
  const response = await apiRequest<{ data: LibraryItemReference[] }>(
    `/api/v1/workspaces/${workspaceId}/library/items/${libraryItemId}/references`,
  );
  return response.data;
}

export type LibraryItemTombstone = {
  library_item_id: string;
  uri: string;
  title: string | null;
  last_version_no: number | null;
  deleted_at: string | null;
};

export async function listLibraryTombstones(workspaceId: string) {
  const response = await apiRequest<{ data: LibraryItemTombstone[] }>(
    `/api/v1/workspaces/${workspaceId}/library/tombstones`,
  );
  return response.data;
}

export async function deleteLibraryItem(
  workspaceId: string,
  libraryItemId: string,
) {
  const response = await apiRequest<{ data: { id: string; deleted: boolean } }>(
    `/api/v1/workspaces/${workspaceId}/library/items/${libraryItemId}`,
    { method: "DELETE" },
  );
  return response.data;
}

export async function uploadLibraryFile(
  workspaceId: string,
  file: File,
  {
    operationId,
    parentNodeId,
    signal,
  }: {
    operationId: string;
    parentNodeId?: string | null;
    signal?: AbortSignal;
  },
) {
  const formData = new FormData();
  formData.set("file", file);
  if (parentNodeId) {
    formData.set("parent_node_id", parentNodeId);
  }

  const headers = new Headers({ Accept: "application/json" });
  headers.set("X-DTX-Library-Upload-Operation-Id", operationId);
  const apiToken = getApiToken();
  if (apiToken) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }

  const response = await fetch(
    `${getApiBaseUrl()}/api/v1/workspaces/${workspaceId}/library/items`,
    {
      method: "POST",
      credentials: "include",
      headers,
      body: formData,
      signal,
    },
  );

  if (!response.ok) {
    throw new LibraryUploadApiError(
      response.status,
      await parseLibraryErrorBody(response),
      retryAfterSeconds(response.headers.get("Retry-After")),
    );
  }

  const body = (await response.json()) as { data: LibraryItemDetail };
  return body.data;
}

/**
 * P10.2: adds a new version onto an EXISTING item -- the explicit
 * `POST .../items/{item_id}/versions` command (backend/api/read.py), never a
 * filename-derived upsert. Identical bytes already on this item are reused
 * (200 from the server); new bytes become a new version (201) and the
 * server sets it as the item's `current_version`.
 */
export async function addLibraryItemVersion(
  workspaceId: string,
  itemId: string,
  file: File,
  {
    operationId,
    signal,
  }: {
    operationId: string;
    signal?: AbortSignal;
  },
) {
  const formData = new FormData();
  formData.set("file", file);

  const headers = new Headers({ Accept: "application/json" });
  headers.set("X-DTX-Library-Upload-Operation-Id", operationId);
  const apiToken = getApiToken();
  if (apiToken) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }

  const response = await fetch(
    `${getApiBaseUrl()}/api/v1/workspaces/${workspaceId}/library/items/${itemId}/versions`,
    {
      method: "POST",
      credentials: "include",
      headers,
      body: formData,
      signal,
    },
  );

  if (!response.ok) {
    throw new LibraryUploadApiError(
      response.status,
      await parseLibraryErrorBody(response),
      retryAfterSeconds(response.headers.get("Retry-After")),
    );
  }

  const body = (await response.json()) as { data: LibraryItemDetail };
  return body.data;
}

/**
 * Identifies the same-origin, authorization-protected original-content route.
 * Keeping this allow-list strict prevents an API token from being forwarded to
 * an arbitrary URL supplied through Library metadata.
 */
export function isProtectedLibraryOriginalUrl(
  value: string | null | undefined,
): value is string {
  return normalizeProtectedLibraryOriginalPath(value) !== null;
}

export async function fetchLibraryOriginal(
  originalUrl: string,
  signal?: AbortSignal,
): Promise<LibraryOriginalDownload> {
  const originalPath = normalizeProtectedLibraryOriginalPath(originalUrl);
  if (!originalPath) {
    throw new TypeError("Library original URL is not an authorized API path");
  }

  const headers = new Headers({ Accept: "application/octet-stream" });
  const apiToken = getApiToken();
  if (apiToken) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }

  const response = await fetch(`${getApiBaseUrl()}${originalPath}`, {
    method: "GET",
    credentials: "include",
    headers,
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseLibraryErrorBody(response));
  }

  return {
    blob: await response.blob(),
    filename: contentDispositionFilename(response.headers.get("Content-Disposition")),
  };
}

function normalizeProtectedLibraryOriginalPath(
  value: string | null | undefined,
) {
  if (!value) return null;
  const path = value.trim();
  return PROTECTED_LIBRARY_ORIGINAL_PATH.test(path) ? path : null;
}

function contentDispositionFilename(value: string | null) {
  if (!value) return null;
  const encoded = value.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      const decoded = decodeURIComponent(stripHeaderQuotes(encoded.trim()));
      const filename = safeDownloadFilename(decoded);
      if (filename) return filename;
    } catch {
      // Fall back to the ASCII filename parameter below.
    }
  }
  const fallback = value.match(/filename\s*=\s*(?:"([^"]*)"|([^;]+))/i);
  return safeDownloadFilename((fallback?.[1] ?? fallback?.[2] ?? "").trim());
}

function stripHeaderQuotes(value: string) {
  return value.startsWith('"') && value.endsWith('"')
    ? value.slice(1, -1)
    : value;
}

function safeDownloadFilename(value: string) {
  const filename = value
    .replace(/\\/g, "/")
    .split("/")
    .at(-1)
    ?.replace(/[\u0000-\u001f\u007f]/g, "")
    .trim();
  return filename || null;
}

async function parseLibraryErrorBody(response: Response): Promise<ApiErrorBody> {
  const text = await response.text();
  if (text) {
    try {
      return JSON.parse(text) as ApiErrorBody;
    } catch {
      return fallbackUploadErrorBody(text);
    }
  }
  return fallbackUploadErrorBody(`Request failed with ${response.status}`);
}

function fallbackUploadErrorBody(message: string): ApiErrorBody {
  return {
    error_code: "internal_error",
    message,
    request_id: "",
    warnings: [],
  };
}

export type LibrarySemanticMarkdown = {
  library_item_id: string;
  version_id: string;
  base_artifact_id: string;
  base_content_sha256: string;
  overlay_id: string | null;
  revision: number;
  source: "base" | "overlay";
  body_markdown: string;
  content_sha256: string;
  byte_size: number;
  updated_at: string;
};

export type LibrarySemanticMarkdownUpdate = {
  body_markdown: string;
  expected_revision: number;
  expected_base_artifact_id: string;
  expected_base_content_sha256: string;
};

export async function getLibrarySemanticMarkdown(
  workspaceId: string,
  versionId: string,
  signal?: AbortSignal,
) {
  const response = await apiRequest<{ data: LibrarySemanticMarkdown }>(
    librarySemanticMarkdownPath(workspaceId, versionId),
    { signal },
  );
  return response.data;
}

export async function updateLibrarySemanticMarkdown(
  workspaceId: string,
  versionId: string,
  update: LibrarySemanticMarkdownUpdate,
  signal?: AbortSignal,
) {
  const response = await apiRequest<{ data: LibrarySemanticMarkdown }>(
    librarySemanticMarkdownPath(workspaceId, versionId),
    {
      method: "PUT",
      body: JSON.stringify(update),
      signal,
    },
  );
  return response.data;
}

function librarySemanticMarkdownPath(workspaceId: string, versionId: string) {
  return `/api/v1/workspaces/${workspaceId}/library/versions/${versionId}/semantic-markdown`;
}

function libraryUploadErrorIsRetryable(status: number, errorCode: string) {
  if (errorCode === "library_upload_idempotency_conflict") return false;
  return status >= 500 ||
    status === 408 ||
    status === 425 ||
    status === 429 ||
    errorCode === "library_upload_policy_changed" ||
    errorCode === "media_install_in_progress";
}

function retryAfterSeconds(value: string | null) {
  if (value === null || !/^\d{1,6}$/.test(value.trim())) return null;
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) && seconds >= 0 && seconds <= 86_400
    ? seconds
    : null;
}

function legacyFormat(
  formatId: string,
  extensions: string[],
  mediaTypes: string[],
): LibraryFormatCapability {
  const previewKind = formatId === "json" ? "metadata" : "markdown";
  return {
    format_id: formatId,
    display_name: formatId.toUpperCase(),
    category: formatId === "csv" || formatId === "json" || formatId === "yaml"
      ? "data"
      : "text",
    extensions,
    default_extensions: [...extensions],
    extensions_overridden: false,
    media_types: mediaTypes,
    search_aliases: [
      formatId,
      ...extensions.map((extension) => extension.replace(/^\./, "")),
      ...mediaTypes,
    ],
    provider_ids: [],
    upload: true,
    conversion: "available",
    preview_kind: previewKind,
    admission_state: "ready",
    storage_state: "available",
    conversion_state: "available",
    preview_state: "ready",
    render_state: "available",
    reason: null,
    renderer_contract: `core.${previewKind}.v1`,
    // The legacy endpoint owns its workspace-specific limit. Avoid inventing a
    // stricter client cap when the additive capabilities endpoint is absent.
    max_bytes: Number.MAX_SAFE_INTEGER,
    max_upload_bytes: Number.MAX_SAFE_INTEGER,
    max_convertible_bytes: Number.MAX_SAFE_INTEGER,
    policy_enabled: true,
    deployment_available: true,
    converter_available: true,
    upload_limit_sources: { workspace: Number.MAX_SAFE_INTEGER },
    upload_limiting_source: "workspace",
  };
}

function normalizeExtension(extension: string) {
  const normalized = extension.trim().toLowerCase();
  if (!normalized) return "";
  return normalized.startsWith(".") ? normalized : `.${normalized}`;
}
