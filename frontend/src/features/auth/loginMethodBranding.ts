import type { CSSProperties } from "react";

import type { LoginMethod } from "../../shared/api/auth";

export function loginMethodLabel(method: LoginMethod, language: string): string {
  return providerLabel(method.display_name, method.label, method.slug, language);
}

// Explicit operator name, then the plugin label for the viewer's locale, its
// base language and English, then the slug — wherever a provider is named.
export function providerLabel(
  name: string | null | undefined,
  labels: Record<string, string>,
  slug: string,
  language: string,
): string {
  return [name, labels[language], labels[language.split("-")[0]], labels.en]
    .find((label) => label?.trim())?.trim() || slug;
}

export function loginMethodStyle(method: LoginMethod): CSSProperties | undefined {
  const isColor = (value: unknown): value is string =>
    typeof value === "string" && /^#[0-9a-f]{6}$/iu.test(value);
  if (!isColor(method.background) || !isColor(method.foreground)) return undefined;
  return { backgroundColor: method.background, color: method.foreground };
}
