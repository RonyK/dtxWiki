import { BROWSER_ROUTES, IDENTITY_QUERY_KEYS } from "./browserRoutes.gen";
import { isWorkspaceUuid } from "./workspaceIdentity";

type BrowserRoute = {
  readonly path: string;
  readonly section: string;
  readonly kind: string;
  readonly targetParam: string | null;
  readonly create: boolean;
  readonly choices: Readonly<Record<string, readonly string[]>>;
};

export type BrowserRouteMatch = {
  route: BrowserRoute;
  params: Readonly<Record<string, string>>;
};

/** Match exact registered paths without normalizing authored dot segments. */
export function matchBrowserRoute(pathname: string): BrowserRouteMatch | null {
  if (/[\\\u0000-\u0020\u007f]|%(?:2f|5c|2e|25|00)|%(?![0-9a-f]{2})/i.test(pathname)) return null;
  let decoded: string;
  try { decoded = decodeURIComponent(pathname); } catch { return null; }
  if (decoded.includes("//")) return null;
  const parts = decoded.replace(/\/$/, "").split("/");
  for (const route of Object.values(BROWSER_ROUTES) as readonly BrowserRoute[]) {
    const expected = route.path.split("/");
    if (expected.length !== parts.length) continue;
    const params: Record<string, string> = {};
    const valid = expected.every((pattern, index) => {
      const value = parts[index];
      if (!pattern.startsWith(":")) return !/[^\u0000-\u007f]/.test(value) && value.toLowerCase() === pattern.toLowerCase();
      const name = pattern.slice(1);
      const choices = route.choices[name];
      if (choices) {
        if (!choices.includes(value)) return false;
        params[name] = value;
      } else {
        if (!isWorkspaceUuid(value)) return false;
        params[name] = value.toLowerCase();
      }
      return true;
    });
    if (valid) return { route, params };
  }
  return null;
}

/** The route decides whether parent is meaningful; presentation keys stay free. */
export function hasConflictingBrowserQuery(search: string, allowParent = false): boolean {
  const params = new URLSearchParams(search);
  return IDENTITY_QUERY_KEYS.some((key) => params.has(key)) ||
    (!allowParent && params.has("parent")) || params.getAll("parent").length > 1;
}

export function isSafeBrowserQuery(search: string, pathname = ""): boolean {
  const params = new URLSearchParams(search);
  const match = matchBrowserRoute(pathname);
  if (hasConflictingBrowserQuery(search, match?.route.create === true)) return false;
  const parent = params.get("parent");
  return parent === null || isWorkspaceUuid(parent);
}

export function isWorkspaceNavigationPath(pathname: string, search: string): boolean {
  return matchBrowserRoute(pathname) !== null && isSafeBrowserQuery(search, pathname);
}
