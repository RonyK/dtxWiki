export type ContextLinkKind =
  | "page_id" | "stable_full" | "stable_short" | "canonical_route"
  | "nav_target" | "concept_path" | "slug" | "resource_uri" | "wikilink"
  | "unknown" | "external" | "public_reader" | "local" | "media";

export type ContextLinkTarget = {
  locator: string;
  href: string | null;
  target_page_id: string | null;
  state: "ok" | "unavailable";
};

export type ContextLinkOptions = {
  workspaceId?: string | null;
  selfOrigins?: readonly string[];
};

export type ContextLink = {
  kind: ContextLinkKind;
  workspaceId: string | null;
  pageId: string | null;
  fragment: string | null;
  stableUrl: string | null;
  lookup: string | null;
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function isContextId(value: string): boolean {
  return UUID.test(value);
}

export function escapeContextLinkLabel(title: string): string {
  return title.replace(/[\\`*_[\]<>!&~]/g, "\\$&");
}

export function normalizeContextHeading(value: string): string {
  return value.trim().toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function fragmentSuffix(fragment?: string | null): string {
  return fragment ? `#${fragment.replace(/^#/, "")}` : "";
}

export function formatContextPageUrl(workspaceId: string, pageId: string, fragment?: string | null): string {
  return `/p/${encodeURIComponent(workspaceId)}/${encodeURIComponent(pageId)}${fragmentSuffix(fragment)}`;
}

export function formatCanonicalContextUrl(workspaceId: string, pageId: string, fragment?: string | null): string {
  return `/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}${fragmentSuffix(fragment)}`;
}

export function formatContextPageMarkdown(title: string, workspaceId: string, pageId: string, fragment?: string | null): string {
  return `[${escapeContextLinkLabel(title)}](${formatContextPageUrl(workspaceId, pageId, fragment)})`;
}

export function classifyContextLink(value: string, options: ContextLinkOptions = {}): ContextLink {
  const empty: ContextLink = {
    kind: "unknown", workspaceId: null, pageId: null, fragment: null,
    stableUrl: null, lookup: null,
  };
  const result = (kind: ContextLinkKind, extra: Partial<ContextLink> = {}): ContextLink => ({ ...empty, kind, ...extra });
  const wiki = /^\[\[([^\]|\n]+)(?:\|[^\]\n]*)?\]\]$/.exec(value);
  if (wiki) {
    const [lookup, heading = ""] = wiki[1].split("#", 2);
    return result(lookup.trim() ? "wikilink" : "local", {
      lookup: lookup.normalize("NFKC").trim().toLowerCase()
        .replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 80),
      fragment: normalizeContextHeading(heading) || null,
    });
  }
  if (!value || value !== value.trim() ||
      Array.from(value).some((character) => character.charCodeAt(0) <= 32) ||
      value.includes("\\") || value.startsWith("//")) return empty;
  if (UUID.test(value)) {
    const pageId = value.toLowerCase();
    return result("page_id", {
      pageId,
      stableUrl: options.workspaceId ? formatContextPageUrl(options.workspaceId, pageId) : null,
    });
  }
  let input = value;
  const absolute = /^https?:\/\//i.test(input);
  if (absolute) {
    let url: URL;
    try { url = new URL(input); } catch { return empty; }
    if (url.username || url.password) return empty;
    const selfOrigins = options.selfOrigins ?? (typeof window === "undefined" ? [] : [window.location.origin]);
    if (!selfOrigins.some((origin) => {
      try { return new URL(origin).origin === url.origin; } catch { return false; }
    })) return result("external");
    // URL.pathname normalizes dot segments. Inspect the authored path first.
    input = /^https?:\/\/[^/?#]*(.*)$/i.exec(value)?.[1] || "/";
  } else if (/^[a-z][a-z0-9+.-]*:/i.test(input) && !input.startsWith("dtxwiki://")) {
    return result(/^(?:dtx-media|context-media|asset):/i.test(input) ? "media" : "external");
  }
  const hashAt = input.indexOf("#");
  const fragment = hashAt < 0 ? null : input.slice(hashAt + 1) || null;
  const withoutHash = hashAt < 0 ? input : input.slice(0, hashAt);
  const queryAt = withoutHash.indexOf("?");
  let pathname = queryAt < 0 ? withoutHash : withoutHash.slice(0, queryAt);
  const query = queryAt < 0 ? "" : withoutHash.slice(queryAt + 1);
  if (/%(?:2f|5c|2e|25|00)/i.test(pathname) || /%(?![0-9a-fA-F]{2})/.test(pathname)) return empty;
  try { pathname = decodeURIComponent(pathname); } catch { return empty; }
  if (pathname.split("/").some((part) => part === "." || part === "..") ||
      Array.from(pathname).some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)) return empty;
  if (input.startsWith("#")) return result("local", { fragment });
  if (/^\/w\/[^/]+\/pages\/[^/]+$/.test(pathname)) return result("public_reader", { fragment });
  if (/^\/(?:api|uploads)\//.test(pathname)) return result("media", { fragment });
  if (query && pathname !== "/context" && /^\/(?:p|context|workspaces|pages)\//.test(pathname)) return result("unknown", { fragment });
  const withPage = (kind: ContextLinkKind, workspaceId: string | null, pageId: string): ContextLink => {
    const resolvedWorkspaceId = workspaceId ?? options.workspaceId;
    return result(kind, {
      workspaceId, pageId: pageId.toLowerCase(), fragment,
      stableUrl: resolvedWorkspaceId ? formatContextPageUrl(resolvedWorkspaceId, pageId.toLowerCase(), fragment) : null,
    });
  };
  let match = /^\/p\/([^/]+)\/([^/]+)$/.exec(pathname);
  if (match && UUID.test(match[1]) && UUID.test(match[2])) return withPage("stable_full", match[1].toLowerCase(), match[2]);
  match = /^\/p\/([^/]+)$/.exec(pathname);
  if (match && UUID.test(match[1])) return withPage("stable_short", null, match[1]);
  match = /^\/workspaces\/([^/]+)\/context\/pages\/([^/]+)(?:\/edit)?$/.exec(pathname);
  if (match && UUID.test(match[1]) && UUID.test(match[2])) return withPage("canonical_route", match[1].toLowerCase(), match[2]);
  if (pathname === "/context" && query) {
    const targets = new URLSearchParams(query).getAll("target");
    if (targets.length === 1 && UUID.test(targets[0])) return result("nav_target", { lookup: targets[0].toLowerCase(), fragment });
  }
  match = /^\/context\/(?:[^/]+\/)*([^/]+)\.md$/.exec(pathname);
  if (match) return result("concept_path", { lookup: match[1].normalize("NFKC").trim(), fragment });
  match = /^\/pages\/([^/]+)$/.exec(pathname);
  if (match) return result("slug", { lookup: match[1].normalize("NFKC").trim(), fragment });
  match = /^dtxwiki:\/\/workspaces\/([^/]+)\/context\/pages\/([^/]+)$/.exec(pathname);
  if (match && UUID.test(match[1])) return result("resource_uri", {
    workspaceId: match[1].toLowerCase(), lookup: match[2].normalize("NFKC").trim(), fragment,
  });
  return result(absolute || /^\/(?:p|context|workspaces|pages)(?:\/|$)/.test(pathname) || input.startsWith("dtxwiki:")
    ? "unknown" : "external", { fragment });
}
