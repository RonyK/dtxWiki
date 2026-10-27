// OAuth consent is rendered by the backend; it needs a document navigation.
export function isNonSpaAuthPath(path: string) {
  return path.startsWith("/oauth/");
}

export function safeNextPath(next: string | null) {
  if (!next || new TextEncoder().encode(next).length > 2048 || /[\\\u0000-\u0020\u007f]/u.test(next)) return "/dashboard";
  let decoded = next.split(/[?#]/u, 1)[0];
  for (let count = 0; count < 8; count += 1) {
    if (!decoded.startsWith("/") || decoded.startsWith("//") || /[\\\u0000-\u0020\u007f]/u.test(decoded)) return "/dashboard";
    const normalized = new URL(decoded, "https://dtxwiki.invalid").pathname.toLowerCase();
    if (normalized === "/api" || normalized.startsWith("/api/")) return "/dashboard";
    try {
      const value = decodeURIComponent(decoded);
      if (value === decoded) return next;
      decoded = value;
    } catch { return "/dashboard"; }
  }
  return "/dashboard";
}
