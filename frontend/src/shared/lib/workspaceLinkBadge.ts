import { classifyContextLink, isContextId } from "./contextLinks";
import { matchBrowserRoute } from "./browserRoutes";

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
  const classification = classifyContextLink(href, options);

  if (
    classification.kind === "stable_full" ||
    classification.kind === "canonical_route"
  ) {
    return differentWorkspace(classification.workspaceId, sourceWorkspaceId);
  }
  if (classification.kind !== "workspace_navigation") return null;

  const route = matchBrowserRoute(parsed.pathname);
  return differentWorkspace(route?.params.workspaceId ?? null, sourceWorkspaceId);
}

function differentWorkspace(
  destinationWorkspaceId: string | null,
  sourceWorkspaceId: string,
): string | null {
  if (!destinationWorkspaceId || !isContextId(destinationWorkspaceId)) return null;
  const destination = destinationWorkspaceId.toLowerCase();
  return destination === sourceWorkspaceId.toLowerCase() ? null : destination;
}
