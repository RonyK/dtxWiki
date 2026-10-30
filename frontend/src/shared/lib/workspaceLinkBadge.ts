import { classifyContextLink, isContextId } from "./contextLinks";
import { parseWorkspaceRoute } from "./workspaceRoutes";

const UUID_PATH = /^\/p\/[0-9a-f-]{36}\/[0-9a-f-]{36}$/i;

/** Return the destination workspace for a recognized same-origin product URL. */
export function workspaceLinkDestinationId(
  href: string,
  sourceWorkspaceId: string | null | undefined,
  origin = typeof window === "undefined" ? null : window.location.origin,
): string | null {
  if (!sourceWorkspaceId || !isContextId(sourceWorkspaceId)) return null;
  if (
    !href ||
    href !== href.trim() ||
    href.startsWith("//") ||
    href.includes("\\") ||
    Array.from(href).some((character) => character.charCodeAt(0) <= 32)
  ) {
    return null;
  }

  const absolute = /^https?:\/\//i.test(href);
  let parsed: URL;
  try {
    if (absolute) {
      if (!origin) return null;
      parsed = new URL(href);
      if (parsed.username || parsed.password || parsed.origin !== new URL(origin).origin) {
        return null;
      }
    } else {
      parsed = new URL(href, origin ?? "http://dtxwiki.invalid");
    }
  } catch {
    return null;
  }

  const selfOrigins = origin ? [origin] : [];
  const options = {
    workspaceId: sourceWorkspaceId,
    selfOrigins,
    allowWorkspaceNavigation: true,
  } as const;
  let classification = classifyContextLink(href, options);

  // Context page URLs may carry harmless view state. The shared Context
  // classifier keeps query-bearing /p URLs unknown, so classify the exact
  // route separately after rejecting identity overrides.
  if (
    classification.kind === "unknown" &&
    UUID_PATH.test(parsed.pathname) &&
    parsed.searchParams.size > 0 &&
    !hasIdentityQuery(parsed.search)
  ) {
    const withoutQuery = removeQuery(href);
    classification = classifyContextLink(withoutQuery, options);
  }

  if (
    classification.kind === "stable_full" ||
    classification.kind === "canonical_route"
  ) {
    return differentWorkspace(classification.workspaceId, sourceWorkspaceId);
  }
  if (classification.kind !== "workspace_navigation") return null;

  // The shared classifier has already checked the exact route grammar and
  // identity-query policy; parseWorkspaceRoute supplies its workspace ID.
  const route = parseWorkspaceRoute(parsed.pathname);
  return differentWorkspace(route?.workspaceId ?? null, sourceWorkspaceId);
}

function differentWorkspace(
  destinationWorkspaceId: string | null,
  sourceWorkspaceId: string,
): string | null {
  if (!destinationWorkspaceId || !isContextId(destinationWorkspaceId)) return null;
  const destination = destinationWorkspaceId.toLowerCase();
  return destination === sourceWorkspaceId.toLowerCase() ? null : destination;
}

function hasIdentityQuery(search: string): boolean {
  const params = new URLSearchParams(search);
  return [
    "target",
    "edit",
    "review",
    "finding",
    "new",
    "newFolder",
    "parentNodeId",
    "parentId",
    "workspaceId",
    "workspace_id",
    "ws",
    "parent",
  ].some((key) => params.has(key));
}

function removeQuery(href: string): string {
  const queryIndex = href.indexOf("?");
  if (queryIndex < 0) return href;
  const fragmentIndex = href.indexOf("#", queryIndex);
  return fragmentIndex < 0
    ? href.slice(0, queryIndex)
    : `${href.slice(0, queryIndex)}${href.slice(fragmentIndex)}`;
}
