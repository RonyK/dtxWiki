// E2E-03: what a failed request is allowed to leave behind in a Playwright
// artifact.
//
// Failed-request records reach `playwright-report/data/`, which BOTH
// .github/workflows/ci.yml and .github/workflows/e2e-browsers.yml upload as a
// 14-day artifact. A URL carries secrets in its query string or fragment --
// the suite itself drives `/reset-password#token=<one-time token>`, and links
// mailed before M-2 use `?token=` -- so only the non-secret shape (method,
// origin, pathname) is ever retained, and the shape is produced by
// `new URL()`, which makes keeping the query or fragment structurally
// impossible rather than merely unintended.
//
// PATH SEGMENTS ARE RETAINED, and the precondition that makes that safe is:
// no route in this application puts a secret in a path segment. Every
// credential-bearing route carries it in the fragment or the query string
// instead (`/reset-password#token=`, `/verify-email#token=`, and the legacy
// `?token=` form of both). Nothing in THIS file can
// establish that -- the route table is the authority for it, and a static
// guard over the route table is where the tie belongs. Stated here so the
// precondition is a checkable claim rather than an assumption.
//
// `CREDENTIAL_PREFIXES` is the cheap, code-side half of that check. It is the
// set of prefixes this application actually mints, and any path segment
// starting with one is replaced before it is retained. That closes the shape a
// reviewer demonstrated live -- a `/mcp/session/<PAT>` request emitted its PAT
// verbatim into the report. It is a NET, NOT A BOUNDARY: a secret with no
// prefix, or one that does not start its own segment, still survives it. Only
// the precondition above makes a path-borne secret impossible; do not read the
// prefix list as replacing it.
//
// The log is bounded and de-duplicated because the shipped version was neither:
// one flapping poll wrote 1001 entries with 2 distinct values into a 44 KB
// attachment. Per-entry `count` plus `dropped_unique_requests` keep the volume
// signal that the raw list carried. `MAX_UNIQUE_RECORDS` bounds how many
// records there can be and `MAX_FIELD_CHARS` bounds how large one record can
// get, because the first alone left a single record unbounded: a single
// record with a 2 MB pathname produced a 2 MB attachment, and the tuple-count
// cap did not touch it.
//
// TypeScript rather than the `.mjs` the plan specified: `tests/e2e` is neither
// type-checked (frontend/tsconfig.json is `include: ["src"]`) nor linted, and a
// sibling `.ts` is what Playwright's transpiler and node's type stripping both
// already handle here -- an `.mjs` would be `require()`d from Playwright's CJS
// output. tests/e2e/redact-network.test.mjs imports this file directly.

/** Distinct (method, origin, pathname, errorText) tuples kept per test. */
export const MAX_UNIQUE_RECORDS = 20;

/**
 * Longest retained `origin` or `pathname`, in characters.
 *
 * For an http(s) URL `new URL()` returns both fields already ASCII -- the host
 * as punycode, the path percent-encoded -- so this is equally a bound in bytes,
 * and neither field can contain a character JSON has to escape. Together with
 * `MAX_UNIQUE_RECORDS`, `SAFE_METHOD` and `SAFE_ERROR_TEXT` that makes the
 * whole attachment size a literal, not a hope.
 *
 * The longest path this suite drives is 37 characters
 * (`/api/v1/auth/password-reset/request`), so no real evidence is truncated.
 * Two paths that agree for the first `MAX_FIELD_CHARS` characters do collapse
 * into one record; at that length they are already both truncated, and the
 * `count` says how many arrived.
 */
export const MAX_FIELD_CHARS = 120;

/** ASCII, so a clamped field is still exactly `MAX_FIELD_CHARS` bytes. */
const TRUNCATION_MARK = "...";

/**
 * Credential prefixes this application mints:
 *
 * - `dtxw_` -- API tokens / MCP PATs (`backend/services/credential_hashing.py`,
 *   `API_TOKEN_PREFIX`)
 * - `dtxr_` -- one-time password-reset tokens, and `dtxe_` -- e-mail
 *   verification tokens (`backend/services/password_recovery_service.py`)
 * - `rt_` -- OAuth refresh tokens (`backend/services/oauth_service.py`)
 *
 * Matched at the START of a path segment only. `rt_` is short enough that
 * matching it anywhere inside a segment would redact ordinary words -- the
 * word "report" contains it -- and a rule that is cheap to check is worth more
 * here than one that is clever.
 */
export const CREDENTIAL_PREFIXES = ["dtxw_", "dtxr_", "dtxe_", "rt_"] as const;

/**
 * Transport error identifiers are short, punctuated ASCII tokens
 * (`net::ERR_NETWORK_CHANGED`, `NS_BINDING_ABORTED`). Anything else is
 * replaced wholesale rather than pattern-scrubbed, because an engine is free
 * to interpolate the failing URL into the text.
 */
const SAFE_ERROR_TEXT = /^[A-Za-z0-9_:.-]{1,64}$/;

/**
 * An HTTP method is a token of letters. `method` arrives from the caller, so
 * without a shape check it is a third unbounded field on a record that is
 * supposed to be bounded.
 */
const SAFE_METHOD = /^[A-Za-z]{1,20}$/;

export type FailedRequestInput = {
  method?: unknown;
  url?: unknown;
  errorText?: unknown;
};

export type RedactedRequest = {
  method: string;
  origin: string;
  pathname: string;
  errorText: string;
};

export type FailedRequestRecord = RedactedRequest & { count: number };

export type FailedRequestReport = {
  requests: FailedRequestRecord[];
  dropped_unique_requests: number;
};

/** Replace any path segment that starts with a credential this app mints. */
export function scrubCredentialPathSegments(pathname: string): string {
  return pathname
    .split("/")
    .map((segment) =>
      CREDENTIAL_PREFIXES.some((prefix) => segment.startsWith(prefix))
        ? "REDACTED"
        : segment,
    )
    .join("/");
}

/**
 * Bound one field, keeping the diagnostic head and marking the cut.
 *
 * Scrubbing runs first, on the whole pathname, so a credential segment is
 * REPLACED rather than truncated into a partial token, and so
 * a segment that becomes `REDACTED` frees room for the rest of the path inside
 * the bound. The ORDER is not itself a security property -- the scrub matches a
 * prefix at the START of a segment, so it fires on a clamped segment just as
 * well. That was mutation-tested rather than assumed: reversing the two behaved
 * identically, and an earlier version of this comment claimed otherwise.
 */
function clamp(value: string): string {
  return value.length <= MAX_FIELD_CHARS
    ? value
    : value.slice(0, MAX_FIELD_CHARS - TRUNCATION_MARK.length) +
        TRUNCATION_MARK;
}

export function redactFailedRequest(
  entry: FailedRequestInput,
): RedactedRequest {
  let origin = "unparsable";
  let pathname = "/";
  try {
    // `new URL` drops userinfo from `origin`, and for an http(s) URL neither
    // `origin` nor `pathname` can carry the query or the fragment.
    const parsed = new URL(String(entry.url));
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      origin = parsed.origin;
      pathname = scrubCredentialPathSegments(parsed.pathname);
    } else {
      // A non-http scheme parses, but its `pathname` is not a path: for
      // `data:text/html,<payload>` it IS the payload, and for `blob:` it is
      // the inner URL. Record the scheme, which is all that is diagnostic
      // here, and nothing else. (This case was found by the self-runner, not
      // reasoned about in advance -- the first draft trusted `new URL`.)
      origin = parsed.protocol;
    }
  } catch {
    // A relative URL. Keep the placeholders rather than throwing out of a
    // fixture teardown.
  }
  const errorText = typeof entry.errorText === "string" ? entry.errorText : "";
  const method = typeof entry.method === "string" ? entry.method : "";
  return {
    method: SAFE_METHOD.test(method) ? method : "UNKNOWN",
    origin: clamp(origin),
    pathname: clamp(pathname),
    errorText: SAFE_ERROR_TEXT.test(errorText) ? errorText : "REDACTED",
  };
}

export type FailedRequestLog = {
  record(entry: FailedRequestInput): void;
  isEmpty(): boolean;
  uniqueCount(): number;
  totalCount(): number;
  toJSON(): FailedRequestReport;
};

export function createFailedRequestLog(
  max: number = MAX_UNIQUE_RECORDS,
): FailedRequestLog {
  const byKey = new Map<string, FailedRequestRecord>();
  let droppedUnique = 0;
  let total = 0;
  return {
    record(entry) {
      const redacted = redactFailedRequest(entry);
      const key = [
        redacted.method,
        `${redacted.origin}${redacted.pathname}`,
        redacted.errorText,
      ].join(" ");
      total += 1;
      const existing = byKey.get(key);
      if (existing) {
        existing.count += 1;
        return;
      }
      if (byKey.size >= max) {
        droppedUnique += 1;
        return;
      }
      byKey.set(key, { ...redacted, count: 1 });
    },
    isEmpty() {
      return total === 0;
    },
    uniqueCount() {
      return byKey.size;
    },
    totalCount() {
      return total;
    },
    toJSON() {
      return {
        requests: [...byKey.values()],
        dropped_unique_requests: droppedUnique,
      };
    },
  };
}
