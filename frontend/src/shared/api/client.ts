import { clearPersistedWorkspaceId } from "./workspace";
import { outgoingContentError } from "./inputPolicy";
import { i18n } from "../i18n";

export type ApiWarning = {
  code: string;
  message: string;
};

export type ApiErrorBody = {
  blockers?: Array<{ code: string; count: number }>;
  error_code: string;
  limit_bytes?: number;
  message: string;
  request_id: string;
  settings_link?: string;
  warnings: ApiWarning[];
  server_version_id?: string;
  server_content_hash?: string;
  // PURGE_AUTHZ_PLAN S-7: only present on the workspace-delete route's 409,
  // and only when the refusal is purge-recoverable (non-empty blockers).
  purge_grant?: string | null;
  purge_grant_expires_at?: string | null;
};

export class ApiError extends Error {
  status: number;
  body: ApiErrorBody;
  displayMessage: string;

  constructor(status: number, body: ApiErrorBody) {
    const displayMessage = formatApiErrorBody(body);
    super(displayMessage);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.displayMessage = displayMessage;
  }
}

type RuntimeConfig = {
  apiBaseUrl?: string;
  apiToken?: string;
};

declare global {
  interface Window {
    __DTXWIKI_RUNTIME_CONFIG__?: RuntimeConfig;
  }
}

const DEFAULT_DEV_BASE_URL = "http://127.0.0.1:21080";
const DEFAULT_PACKAGED_BASE_URL = "";

// Error copy may include server-maintenance commands only for an authenticated
// instance administrator. AuthContext updates this display-only hint from the
// authoritative /auth/me response; authorization remains entirely server-side.
let apiErrorViewerIsServerAdmin = false;

export function setApiErrorViewerIsServerAdmin(isServerAdmin: boolean) {
  apiErrorViewerIsServerAdmin = isServerAdmin;
}

export function getApiBaseUrl() {
  return normalizeBaseUrl(
    getDevApiBaseUrlOverride() ||
      getRuntimeConfig().apiBaseUrl ||
      getDefaultBaseUrl(),
  );
}

export function getApiToken() {
  const configuredToken =
    getDevApiTokenOverride() || getRuntimeConfig().apiToken;
  if (configuredToken) {
    return configuredToken;
  }
  return undefined;
}

// Vite inlines VITE_* values into the bundle, so a token or API origin left in
// a developer's frontend/.env.local would otherwise ship inside the production
// build. These overrides exist for `npm run dev` only; packaged builds use the
// same-origin API and the backend-provided runtime config. Keep each read a
// static `DEV ? value : undefined`: the production build folds it to undefined
// and drops the inlined value.
function getDevApiBaseUrlOverride(): string | undefined {
  return import.meta.env.DEV ? import.meta.env.VITE_API_BASE_URL : undefined;
}

function getDevApiTokenOverride(): string | undefined {
  return import.meta.env.DEV
    ? import.meta.env.VITE_DTXWIKI_API_TOKEN
    : undefined;
}

function getRuntimeConfig(): RuntimeConfig {
  if (typeof window === "undefined") {
    return {};
  }
  return window.__DTXWIKI_RUNTIME_CONFIG__ ?? {};
}

function getDefaultBaseUrl() {
  return import.meta.env.DEV ? DEFAULT_DEV_BASE_URL : DEFAULT_PACKAGED_BASE_URL;
}

function normalizeBaseUrl(baseUrl: string) {
  return baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
}

// Segments that introduce a name the user chose rather than an id we generated.
// Only here can `undefined`/`null` be real content.
const CONTENT_NAME_PARENTS = new Set(["by-slug"]);

/**
 * Reject a request whose path has an empty or placeholder segment before it
 * reaches the network.
 *
 * Screens build workspace-scoped URLs by interpolation, so a missing id yields
 * `/api/v1/workspaces//ask/sessions`. FastAPI cannot match an empty path
 * parameter, so the catch-all answers `not_found` and the user sees "the
 * requested item could not be found" for what is really a client-side state
 * bug. Failing here keeps the real cause visible and costs no round trip.
 */
function assertRequestPath(path: string): void {
  const [pathname] = path.split("?");
  const segments = pathname.split("/");
  // A leading slash and a trailing slash each produce an empty entry that means
  // nothing is missing. Dropping them by position rather than by `slice(1)`
  // keeps `/` and `/api/v1/pages/` legal and stops a relative path from having
  // its first real segment silently discarded.
  if (segments[0] === "") {
    segments.shift();
  }
  if (segments.length > 0 && segments[segments.length - 1] === "") {
    segments.pop();
  }
  // An empty segment is never legitimate anywhere. The literal strings
  // `undefined`/`null` mean "a JS value stringified into the URL" in every
  // position except the ones that carry content-derived names: `SLUG_PATTERN`
  // accepts both, so a page titled "null" slugs to `null`, and rejecting it
  // made that page permanently unopenable (status 0 also dodges the caller's
  // 404 branch, so the UI could only ask the user to reload).
  const bad = segments.findIndex(
    (segment, index) =>
      segment === "" ||
      (!CONTENT_NAME_PARENTS.has(segments[index - 1] ?? "") &&
        (segment === "undefined" || segment === "null")),
  );
  if (bad === -1) {
    return;
  }
  const workspaceScoped = segments[bad - 1] === "workspaces";
  throw new ApiError(0, {
    error_code: workspaceScoped ? "workspace_not_selected" : "invalid_request_path",
    message: `Request path has an unusable segment: ${pathname}`,
    request_id: "",
    warnings: [],
  });
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  assertRequestPath(path);
  const inputError = outgoingContentError(path, init.body);
  if (inputError) {
    throw new ApiError(422, {
      error_code: "validation_failed", message: inputError, request_id: "", warnings: [],
    });
  }
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const apiToken = getApiToken();
  if (apiToken) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    credentials: init.credentials ?? "include",
    headers,
  });

  if (!response.ok) {
    const body = await parseApiErrorBody(response);
    if (response.status === 401 && !isPrivilegedReauthentication(path, body)) {
      handleAuthExpired(path);
    }
    throw new ApiError(response.status, body);
  }

  return (await response.json()) as T;
}

// Server Admin surfaces deliberately return 401 when recent reauthentication is
// missing: the protected directory must relock, and open dialogs must keep or
// request their confirmation. That response never proves that the browser
// session itself expired. Every other non-auth 401 continues through the
// normal session-expiry path.
function isPrivilegedReauthentication(path: string, body: ApiErrorBody) {
  return (
    body.error_code === "recent_reauthentication_required" &&
    path.startsWith("/api/v1/admin/")
  );
}

/**
 * Returns actor-safe UI copy. ApiError has already mapped a stable server code
 * to localized product language; arbitrary Error.message values may contain
 * request, transport, or implementation details and therefore use fallback.
 */
export function apiErrorMessage(error: unknown, fallback?: string) {
  if (error instanceof ApiError) {
    return error.displayMessage;
  }
  return (
    fallback ??
    i18n.t("shell:apiErrors.requestFailed", {
      defaultValue: "Request failed. Please try again.",
    })
  );
}

export const AUTH_EXPIRED_EVENT = "dtxwiki:auth-expired";

// /api/v1/auth/* endpoints 401 in normal flows (the anonymous getMe probe,
// a wrong login/current password), so they must not be treated as an
// expired session.
const AUTH_EXPIRY_EXEMPT_PREFIX = "/api/v1/auth/";
const LOGIN_PATH = "/login";

// A 401 outside the auth endpoints means the session cookie expired or was
// revoked mid-use. Drop the persisted workspace selection (it belongs to the
// dead session), broadcast an event AuthContext can consume, and send the
// user to the login form with the current location preserved as ?next=.
function handleAuthExpired(path: string) {
  if (typeof window === "undefined") {
    return;
  }
  if (getApiToken()) {
    return;
  }
  if (path.startsWith(AUTH_EXPIRY_EXEMPT_PREFIX)) {
    return;
  }
  clearPersistedWorkspaceId();
  window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT, { detail: { path } }));
  if (window.location.pathname.startsWith(LOGIN_PATH)) {
    return;
  }
  const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  window.location.assign(`${LOGIN_PATH}?next=${encodeURIComponent(next)}`);
}

async function parseApiErrorBody(response: Response): Promise<ApiErrorBody> {
  const text = await response.text();
  if (text) {
    try {
      return JSON.parse(text) as ApiErrorBody;
    } catch {
      return fallbackApiErrorBody(text);
    }
  }
  return fallbackApiErrorBody(`Request failed with ${response.status}`);
}

function fallbackApiErrorBody(message: string): ApiErrorBody {
  return {
    error_code: "internal_error",
    message,
    request_id: "",
    warnings: [],
  };
}

function formatApiErrorBody(body: ApiErrorBody) {
  const fallback = i18n.t("shell:apiErrors.requestFailed", {
    defaultValue: "Request failed. Please try again.",
  });
  const errorCode = body.error_code?.trim();
  const localizedErrorCode =
    errorCode === "database_schema_mismatch" && apiErrorViewerIsServerAdmin
      ? "database_schema_mismatch_admin"
      : errorCode;
  const errorKey = localizedErrorCode
    ? `shell:apiErrors.${localizedErrorCode}`
    : "";
  const hasLocalizedMessage = Boolean(errorKey && i18n.exists(errorKey));
  const message = hasLocalizedMessage ? i18n.t(errorKey) : fallback;
  return message;
}
