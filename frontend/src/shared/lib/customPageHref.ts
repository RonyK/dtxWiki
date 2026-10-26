const FORBIDDEN_EXECUTION_SCHEMES = new Set(["javascript", "vbscript", "data"]);

/**
 * Preserve every authored href format except known executable schemes. The
 * browser strips leading C0/space and embedded ASCII tabs/newlines before
 * interpreting a scheme; check that view without changing the authored href.
 * Unrecognized syntax is not evidence that an address should be removed.
 */
export function canPreservePageHref(value: string): boolean {
  const browserPrefix = value.replace(/[\t\n\r]/g, "").replace(/^[\u0000-\u0020]+/, "");
  const scheme = /^([^:/?#]*):/.exec(browserPrefix)?.[1].toLowerCase();
  return !scheme || !FORBIDDEN_EXECUTION_SCHEMES.has(scheme);
}
