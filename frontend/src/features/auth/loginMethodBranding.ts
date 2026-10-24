import type { CSSProperties } from "react";

import type { LoginMethod } from "../../shared/api/auth";

export function loginMethodStyle(method: LoginMethod): CSSProperties | undefined {
  const isColor = (value: unknown): value is string =>
    typeof value === "string" && /^#[0-9a-f]{6}$/iu.test(value);
  if (!isColor(method.background) || !isColor(method.foreground)) return undefined;
  return { backgroundColor: method.background, color: method.foreground };
}
