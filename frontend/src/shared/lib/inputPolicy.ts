import type { ClipboardEvent, CompositionEvent, FormEvent } from "react";

import { i18n } from "../i18n";

export const codePointLength = (value: string) => Array.from(value).length;
const EDGE_SPACES = /^[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+|[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+$/g;
export const trimShortText = (value: string) => value.replace(EDGE_SPACES, "");

export function inputListLimitMessage(maximum: number) {
  return i18n.resolvedLanguage?.startsWith("ko")
    ? `최대 ${maximum}개까지 입력할 수 있습니다.`
    : `Use ${maximum} items or fewer.`;
}

export function inputLimitMessage(maximum: number) {
  return i18n.resolvedLanguage?.startsWith("ko")
    ? `최대 ${maximum.toLocaleString()}자까지 입력할 수 있습니다.`
    : `Use ${maximum.toLocaleString()} characters or fewer.`;
}

export function inputTextError(value: string, maximum: number, singleLine = true) {
  const invalid = singleLine
    ? /[\u0000-\u001f\u007f-\u009f\ud800-\udfff\u2028\u2029]/u
    : /[\u0000\ud800-\udfff]/u;
  if (!invalid.test(value)) {
    const counted = singleLine ? trimShortText(value) : value;
    return codePointLength(counted) > maximum ? inputLimitMessage(maximum) : "";
  }
  return i18n.resolvedLanguage?.startsWith("ko")
    ? "허용되지 않는 문자가 포함되어 있습니다."
    : "This value contains an unsupported character.";
}

export function markdownInputError(value: string) {
  const bodyError = inputTextError(value, 1_000_000, false);
  if (bodyError) return bodyError;
  const heading = value.split(/\r\n|\r|\n/).find((line) => line.trim().startsWith("# "));
  return heading ? inputTextError(heading.trim().slice(2), 256) : "";
}

/** Let IME and paste complete without shortening input; native form validation
 * blocks submission and presents localized feedback for invalid values. */
export function textInputLimit(maximum: number, singleLine = true, normalize = (value: string) => value) {
  type Control = HTMLInputElement | HTMLTextAreaElement;
  function validate(control: Control) {
    const error = inputTextError(control.value, Number.MAX_SAFE_INTEGER, singleLine) || inputTextError(normalize(control.value), maximum, singleLine);
    control.setCustomValidity(error);
    control.setAttribute("aria-invalid", String(Boolean(error)));
  }
  return {
    onInput: (event: FormEvent<Control>) => {
      if (!(event.nativeEvent as InputEvent).isComposing) validate(event.currentTarget);
    },
    onCompositionEnd: (event: CompositionEvent<Control>) => validate(event.currentTarget),
    onPaste: (event: ClipboardEvent<Control>) => {
      const pasted = event.clipboardData.getData("text");
      if (singleLine && /[\u0000-\u001f\u007f-\u009f\ud800-\udfff\u2028\u2029]/u.test(pasted)) {
        event.preventDefault();
        const error = inputTextError("\n", maximum);
        event.currentTarget.setCustomValidity(error);
        event.currentTarget.setAttribute("aria-invalid", "true");
        event.currentTarget.reportValidity();
      }
    },
    onBlur: (event: FormEvent<Control>) => validate(event.currentTarget),
  };
}
