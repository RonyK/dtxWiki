import { ApiError, getApiBaseUrl, getApiToken } from "../../../shared/api/client";

export type PreviewTransportScope = {
  workspaceId: string;
  itemId: string;
  versionId: string;
  allowLegacyRoutes?: boolean;
};

export type PreviewTransport = {
  contentUrl(value: string): string;
  fetch(
    value: string,
    options?: {
      accept?: string;
      range?: string;
      signal?: AbortSignal;
    },
  ): Promise<Response>;
  pdfSource(value: string): {
    url: string;
    httpHeaders?: Record<string, string>;
    withCredentials: true;
  };
  mediaSource(value: string): string | null;
};

export class UnsafePreviewContentUrlError extends TypeError {
  constructor() {
    super("Preview content URL is outside the authorized Library version");
    this.name = "UnsafePreviewContentUrlError";
  }
}

export function createAuthenticatedPreviewTransport(
  scope: PreviewTransportScope,
): PreviewTransport {
  const resolvePath = (value: string) => {
    const path = normalizePreviewContentPath(value, scope);
    if (!path) throw new UnsafePreviewContentUrlError();
    return path;
  };
  return {
    contentUrl(value) {
      return `${getApiBaseUrl()}${resolvePath(value)}`;
    },
    async fetch(value, options = {}) {
      const headers = new Headers({ Accept: options.accept ?? "*/*" });
      const token = getApiToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      if (options.range) headers.set("Range", options.range);
      const response = await globalThis.fetch(
        `${getApiBaseUrl()}${resolvePath(value)}`,
        {
          method: "GET",
          credentials: "include",
          headers,
          signal: options.signal,
        },
      );
      if (!response.ok) {
        throw new ApiError(response.status, {
          error_code: "preview_content_unavailable",
          message: `Preview content request failed with ${response.status}`,
          request_id: "",
          warnings: [],
        });
      }
      return response;
    },
    pdfSource(value) {
      const token = getApiToken();
      return {
        url: `${getApiBaseUrl()}${resolvePath(value)}`,
        ...(token ? { httpHeaders: { Authorization: `Bearer ${token}` } } : {}),
        withCredentials: true,
      };
    },
    mediaSource(value) {
      const path = resolvePath(value);
      // Native media elements cannot attach a Bearer header. Cookie-authenticated
      // same-origin deployments can stream directly; token-only deployments
      // fail to the next manifest variant instead of issuing a doomed request.
      return getApiToken() ? null : `${getApiBaseUrl()}${path}`;
    },
  };
}

// getApiBaseUrl() is "" in every production build (the packaged wheel and the
// E2E harness both serve the SPA and the API from one origin), and `new URL("")`
// throws. There the document origin IS the API origin. Anything we cannot
// resolve to an origin returns null, which rejects the content URL.
function resolveApiOrigin(): string | null {
  const documentOrigin =
    typeof globalThis.location === "undefined"
      ? undefined
      : globalThis.location.origin;
  const baseUrl = getApiBaseUrl();
  if (!baseUrl) return documentOrigin ?? null;
  try {
    return new URL(baseUrl, documentOrigin).origin;
  } catch {
    return null;
  }
}

export function normalizePreviewContentPath(
  value: string,
  scope: PreviewTransportScope,
) {
  const trimmed = value.trim();
  if (!trimmed || trimmed.includes("?") || trimmed.includes("#")) return null;
  let path = trimmed;
  if (/^https?:\/\//i.test(trimmed)) {
    let parsed: URL;
    try {
      parsed = new URL(trimmed);
    } catch {
      return null;
    }
    const apiOrigin = resolveApiOrigin();
    if (!apiOrigin || parsed.origin !== apiOrigin) return null;
    path = parsed.pathname;
  }
  if (!path.startsWith("/") || path.includes("\\") || path.includes("//")) {
    return null;
  }
  const decoded = path.split("/").map((segment) => {
    try {
      return decodeURIComponent(segment);
    } catch {
      return "\0";
    }
  });
  if (decoded.some((segment) => segment === "." || segment === ".." || segment.includes("/"))) {
    return null;
  }
  const libraryPrefix = ["", "api", "v1", "workspaces", scope.workspaceId, "library"];
  if (!libraryPrefix.every((segment, index) => decoded[index] === segment)) {
    return null;
  }
  const rest = decoded.slice(libraryPrefix.length);

  // P9.2: the canonical family is flat -- versions/{versionId}/content|preview
  // (|/source), library-wide artifacts/{artifactId}/content and
  // semantic-overlays/{overlayId}/content. Neither of the latter two carries
  // this scope's itemId/versionId in the URL any more (the server derives
  // and re-authorizes that lineage on every request), so this check is only
  // the workspace/path-family guard against forging a same-origin fetch to
  // an unrelated part of the API -- not a substitute for server authorization.
  if (rest[0] === "versions" && rest[1] === scope.versionId) {
    if (rest.length === 3 && (rest[2] === "content" || rest[2] === "preview")) {
      return path;
    }
    if (rest.length === 4 && rest[2] === "preview" && rest[3] === "source") {
      return path;
    }
  }
  if (
    (rest[0] === "artifacts" || rest[0] === "semantic-overlays") &&
    rest.length === 3 &&
    rest[2] === "content"
  ) {
    return path;
  }

  // Very old flat-on-item shape, retained only for a live manifest-endpoint
  // outage (see PreviewPane's `compatibility` mode).
  if (
    scope.allowLegacyRoutes &&
    rest[0] === scope.itemId &&
    rest.length === 2 &&
    (rest[1] === "preview" || rest[1] === "original")
  ) {
    return path;
  }
  return null;
}
