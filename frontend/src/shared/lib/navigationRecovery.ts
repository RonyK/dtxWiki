import { classifyContextLink, isContextId } from "./contextLinks";
import { parseWorkspaceRoute } from "./workspaceRoutes";

export const REFERENCE_RETURN_STATE_KEY = "dtxwikiReferenceReturnTo";

type ReferenceReturnState = {
  [REFERENCE_RETURN_STATE_KEY]: string;
};

type WorkspaceRouteIdentity = {
  workspaceId: string;
  section: string;
  targetId: string | null;
  targetKind: string | null;
  suffix: string;
};

/** Attach a Markdown source URL to an internal product-link navigation. */
export function createReferenceReturnState(sourceHref: string): ReferenceReturnState {
  return { [REFERENCE_RETURN_STATE_KEY]: sourceHref };
}

export function isWorkspaceProductRoute(href: string): boolean {
  return validatedWorkspaceRoute(href) !== null;
}

/**
 * Read a return destination only when it is a valid Workspace route in the
 * current membership list. React Router state is still untrusted input: never
 * accept an absolute URL, a route outside the Workspace router, the failed
 * route itself, or an identity-conflicting query string.
 */
export function readSafeReferenceReturnTo(
  state: unknown,
  currentHref: string,
  knownWorkspaceIds: readonly string[],
): string | null {
  if (!state || typeof state !== "object") return null;
  const href = (state as Record<string, unknown>)[REFERENCE_RETURN_STATE_KEY];
  if (
    typeof href !== "string" ||
    !href.startsWith("/") ||
    href.startsWith("//") ||
    /[\\\u0000-\u001f\u007f]/.test(href)
  ) {
    return null;
  }

  const sourceRoute = validatedWorkspaceRoute(href);
  if (!sourceRoute) return null;
  const knownIds = new Set(knownWorkspaceIds.filter(isContextId).map((id) => id.toLowerCase()));
  if (!knownIds.has(sourceRoute.workspaceId.toLowerCase())) return null;

  const currentRoute = validatedWorkspaceRoute(currentHref);
  if (currentRoute && isSameRouteEntity(sourceRoute, currentRoute)) return null;

  // URL parsing is deliberately last: classifyContextLink validates the
  // authored path before URL() can normalize dot segments or escaped paths.
  try {
    const sourceUrl = new URL(href, "https://dtxwiki.invalid");
    if (sourceUrl.origin !== "https://dtxwiki.invalid") return null;
    return `${sourceUrl.pathname}${sourceUrl.search}${sourceUrl.hash}`;
  } catch {
    return null;
  }
}

function validatedWorkspaceRoute(href: string): WorkspaceRouteIdentity | null {
  const classified = classifyContextLink(href, {
    allowWorkspaceNavigation: true,
    selfOrigins: [],
  });
  if (classified.kind === "stable_full" && classified.workspaceId && classified.pageId) {
    return {
      workspaceId: classified.workspaceId,
      section: "context",
      targetId: classified.pageId,
      targetKind: "page",
      suffix: `context/pages/${classified.pageId}`,
    };
  }
  if (classified.kind !== "workspace_navigation" && classified.kind !== "canonical_route") {
    return null;
  }
  const route = parseWorkspaceRoute(href.split(/[?#]/, 1)[0]);
  if (!route || !isContextId(route.workspaceId)) return null;
  return route;
}

function isSameRouteEntity(left: WorkspaceRouteIdentity, right: WorkspaceRouteIdentity) {
  if (left.workspaceId.toLowerCase() !== right.workspaceId.toLowerCase()) return false;
  if (left.targetId !== null || right.targetId !== null) {
    return left.targetId !== null &&
      right.targetId !== null &&
      left.section === right.section &&
      left.targetKind === right.targetKind &&
      left.targetId.toLowerCase() === right.targetId.toLowerCase();
  }
  return left.section === right.section && left.suffix === right.suffix;
}
