import type { ClipboardEvent, KeyboardEvent } from "react";

export const ACCOUNT_TRIM = /^[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+|[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+$/gu;
const INVALID_LINE = /[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u;
const INVALID_SCALAR = /[\ud800-\udfff]/u;
const INNER_SPACE = /[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000<>,;"]/u;

export function normalizeAccountEmail(value: string): string {
  if (INVALID_LINE.test(value)) throw new Error("validation.email");
  const normalized = value.replace(ACCOUNT_TRIM, "").toLowerCase();
  const parts = normalized.split("@");
  if (parts.length !== 2 || !parts[0] || !parts[1] || INNER_SPACE.test(normalized)
      || new TextEncoder().encode(normalized).length > 254
      || new TextEncoder().encode(parts[0]).length > 64) {
    throw new Error("validation.email");
  }
  return normalized;
}

export function normalizeAccountName(value: string): string | undefined {
  if (INVALID_LINE.test(value)) throw new Error("validation.displayName");
  const normalized = value.replace(ACCOUNT_TRIM, "");
  if (Array.from(normalized).length > 32) throw new Error("validation.displayName");
  return normalized || undefined;
}

export function validateAccountPassword(value: string, isNew = false): void {
  const length = Array.from(value).length;
  if (INVALID_SCALAR.test(value) || length < (isNew ? 8 : 1) || length > 128) {
    throw new Error(isNew ? "validation.passwordNew" : "validation.passwordCurrent");
  }
}

export function rejectMultilinePaste(event: ClipboardEvent<HTMLInputElement>, reject: () => void): void {
  if (INVALID_LINE.test(event.clipboardData.getData("text"))) {
    event.preventDefault();
    reject();
  }
}

export function preventCompositionSubmit(event: KeyboardEvent<HTMLElement>): void {
  if (event.key === "Enter" && (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)) {
    event.preventDefault();
  }
}
