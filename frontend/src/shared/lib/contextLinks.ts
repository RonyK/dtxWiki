import { isAppLinkPath, isSafeBrowserQuery, isWorkspaceNavigationPath, legacyWorkspaceEntryPath, matchBrowserRoute } from "./browserRoutes";

export type ContextLinkKind =
  | "page_id" | "stable_full" | "stable_short" | "canonical_route"
  | "nav_target" | "resource_uri" | "wikilink"
  | "workspace_navigation" | "unknown" | "external" | "public_reader" | "local" | "media";

// The REST projection as served: an omitted href/target_page_id means null.
// Readers pass response arrays through unchanged, so their identity stays
// stable across renders and MarkdownView can reuse its rendered tree.
export type ContextLinkTarget = {
  locator: string;
  href?: string | null;
  target_page_id?: string | null;
  state: "ok" | "unavailable";
};

export type ContextLinkOptions = {
  workspaceId?: string | null;
  selfOrigins?: readonly string[];
  /** Allow explicit typed product routes in trusted authored previews/readers. */
  allowWorkspaceNavigation?: boolean;
};

export type ContextLink = {
  kind: ContextLinkKind;
  workspaceId: string | null;
  pageId: string | null;
  fragment: string | null;
  stableUrl: string | null;
  navigationHref: string | null;
  edit: boolean;
  lookup: string | null;
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function isContextId(value: string): boolean {
  return UUID.test(value);
}

export function escapeContextLinkLabel(title: string): string {
  // CommonMark backslash escapes keep label text literal in the page's
  // extended Markdown profile too, including dollar-delimited inline math.
  return title.replace(/[!-/:-@[-`{-~]/g, "\\$&");
}

export function normalizeContextHeading(value: string): string {
  return value.trim().toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

// The shared anchor namespace. rehype-sanitize clobber-prefixes every id/name
// with this so user-authored markup can never claim (clobber) the app's own
// anchor/DOM namespace; rehypePrefixFragmentHrefs keeps same-page fragment
// links pointing at the prefixed ids.
export const CONTEXT_HEADING_ID_PREFIX = "user-content-";

/** Match heading IDs produced by the page renderer's sanitizer. */
export function withContextHeadingFragment(href: string): string {
  const index = href.indexOf("#");
  if (
    index < 0 || index === href.length - 1 ||
    href.slice(index + 1).startsWith(CONTEXT_HEADING_ID_PREFIX)
  ) return href;
  return `${href.slice(0, index + 1)}${CONTEXT_HEADING_ID_PREFIX}${href.slice(index + 1)}`;
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

/** Identify authored app paths without turning invalid paths into locators. */
export function isAppLink(value: string, options: ContextLinkOptions = {}): boolean {
  // Match URI parsing for recognition only; classifyContextLink still rejects
  // authored whitespace, controls and encoded separators as valid locators.
  const href = value.replace(/^[\u0000-\u0020]+/, "").replace(/[\r\n\t]/g, "");
  let pathname = href;
  if (/^https?:\/\//i.test(href)) {
    let url: URL;
    try { url = new URL(href); } catch { return false; }
    const selfOrigins = options.selfOrigins ?? (typeof window === "undefined" ? [] : [window.location.origin]);
    if (!selfOrigins.some((origin) => {
      try { return new URL(origin).origin === url.origin; } catch { return false; }
    })) return false;
    pathname = /^https?:\/\/[^/?#]*(.*)$/i.exec(href)?.[1] || "/";
  } else if (!href.startsWith("/") || href.startsWith("//")) {
    return false;
  }
  pathname = pathname.split(/[?#]/, 1)[0];
  try {
    // URI decoding preserves malformed percent escapes, but rejects bad UTF-8.
    pathname = decodeURIComponent(pathname.replace(/%(?![0-9a-f]{2})/gi, "%25"));
  } catch { return false; }
  return isAppLinkPath(pathname);
}

export function classifyContextLink(value: string, options: ContextLinkOptions = {}): ContextLink {
  const empty: ContextLink = {
    kind: "unknown", workspaceId: null, pageId: null, fragment: null,
    stableUrl: null, navigationHref: null, edit: false, lookup: null,
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
  const rawPathname = pathname;
  const query = queryAt < 0 ? "" : withoutHash.slice(queryAt + 1);
  if (/^dtxwiki:\/\//i.test(input) && queryAt >= 0) {
    return result("unknown", { fragment });
  }
  if (/%(?:2f|5c|2e|25|00)/i.test(pathname) || /%(?![0-9a-fA-F]{2})/.test(pathname)) return empty;
  try { pathname = decodeURIComponent(pathname); } catch { return empty; }
  if (pathname.split("/").some((part) => part === "." || part === "..") ||
      Array.from(pathname).some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)) return empty;
  if (input.startsWith("#")) return result("local", { fragment });
  if (/^\/w\/[^/]+\/pages\/[^/]+$/.test(pathname)) return result("public_reader", { fragment });
  if (/^\/(?:api|uploads)\//.test(pathname)) return result("media", { fragment });
  const withPage = (kind: ContextLinkKind, workspaceId: string | null, pageId: string, edit = false): ContextLink => {
    const resolvedWorkspaceId = workspaceId ?? options.workspaceId;
    return result(kind, {
      workspaceId, pageId: pageId.toLowerCase(), fragment, edit,
      stableUrl: resolvedWorkspaceId ? formatContextPageUrl(resolvedWorkspaceId, pageId.toLowerCase(), fragment) : null,
      navigationHref: kind === "canonical_route" ? input : null,
    });
  };
  const browserRoute = matchBrowserRoute(rawPathname);
  if (browserRoute?.route.section === "context" && browserRoute.route.kind === "page") {
    if ((query && !options.allowWorkspaceNavigation) || !isSafeBrowserQuery(query, rawPathname)) return result("unknown", { fragment });
    return withPage("canonical_route", browserRoute.params.workspaceId, browserRoute.params.pageId, browserRoute.route.edit);
  }
  if (options.allowWorkspaceNavigation && query && isWorkspaceNavigationPath(rawPathname, query)) {
    return result("workspace_navigation", {
      stableUrl: input, fragment,
      navigationHref: legacyWorkspaceEntryPath(rawPathname, query) !== null ? input : null,
    });
  }
  if (query && pathname !== "/context" && /^\/(?:p|context|workspaces|pages)\//.test(pathname)) return result("unknown", { fragment });
  let match = /^\/p\/([^/]+)\/([^/]+)$/.exec(pathname);
  if (match && UUID.test(match[1]) && UUID.test(match[2])) return withPage("stable_full", match[1].toLowerCase(), match[2]);
  match = /^\/p\/([^/]+)$/.exec(pathname);
  if (match && UUID.test(match[1])) return withPage("stable_short", null, match[1]);
  if (options.allowWorkspaceNavigation && isWorkspaceNavigationPath(rawPathname, query)) {
    return result("workspace_navigation", {
      stableUrl: input, fragment,
      navigationHref: legacyWorkspaceEntryPath(rawPathname, query) !== null ? input : null,
    });
  }
  if (pathname === "/context" && query) {
    const targets = new URLSearchParams(query).getAll("target");
    if (targets.length === 1 && UUID.test(targets[0])) return result("nav_target", { lookup: targets[0].toLowerCase(), fragment });
  }
  match = /^dtxwiki:\/\/workspaces\/([^/]+)\/context\/pages\/([^/]+)$/.exec(pathname);
  if (match && UUID.test(match[1])) return result("resource_uri", {
    workspaceId: match[1].toLowerCase(), lookup: match[2].normalize("NFKC").trim(), fragment,
  });
  return result(absolute || /^\/(?:p|context|workspaces|pages|library|agent|review|chats|ask|lint|dashboard|search|activity|settings|setup)(?:\/|$)/.test(pathname) || input.startsWith("dtxwiki:")
    ? "unknown" : "external", { fragment });
}

export function contextNavigationHref(href: string, workspaceId: string): string | null {
  if (/[\\\u0000-\u0020\u007f]/.test(href) || href !== href.trim() || href.startsWith("//")) return null;
  const options = {
    workspaceId,
    allowWorkspaceNavigation: true,
  };
  let classification = classifyContextLink(href, options);
  const hashAt = href.indexOf("#");
  const queryAt = href.indexOf("?");
  if (classification.kind === "unknown" && queryAt >= 0 && (hashAt < 0 || queryAt < hashAt)) {
    const query = href.slice(queryAt, hashAt < 0 ? undefined : hashAt);
    if (isSafeBrowserQuery(query, href.slice(0, queryAt))) {
      const withoutQuery = href.slice(0, queryAt) + (hashAt < 0 ? "" : href.slice(hashAt));
      const route = classifyContextLink(withoutQuery, options);
      if (["stable_full", "stable_short", "canonical_route"].includes(route.kind)) classification = route;
    }
  }
  const recognized = classification.kind === "workspace_navigation" ||
    classification.kind === "canonical_route" ||
    classification.kind === "stable_full" ||
    classification.kind === "stable_short";
  if (!recognized) return null;
  if (classification.navigationHref) return classification.navigationHref;
  // Classify the authored path before stripping the origin: URL.pathname
  // would silently normalize dot segments that must remain rejected.
  return /^https?:\/\//i.test(href)
    ? /^https?:\/\/[^/?#]*(.*)$/i.exec(href)?.[1] || "/"
    : href;
}
