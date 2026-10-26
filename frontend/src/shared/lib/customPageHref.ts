const URI_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const FORBIDDEN_EXECUTION_SCHEMES = new Set(["javascript", "vbscript", "data"]);

/**
 * Whether an authored page anchor href can retain its URI scheme through the
 * page renderer's sanitizer. This deliberately validates only the scheme and
 * unsafe characters; it does not parse, normalize, or rewrite the URI.
 */
export function canPreservePageHref(value: string): boolean {
  if (!value || value !== value.trim() || /[\u0000-\u0020\u007f\\]/.test(value)) {
    return false;
  }

  const match = URI_SCHEME.exec(value);
  if (!match) return false;

  return !FORBIDDEN_EXECUTION_SCHEMES.has(match[0].slice(0, -1).toLowerCase());
}
